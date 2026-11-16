import logging
from datetime import date, timedelta

from django.db import transaction
from django.db.models import Case, DecimalField, F, FloatField, Prefetch, Q, Value, When
from django.db.models.functions import Cast, Lower, NullIf
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics, permissions, serializers, status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from apps.ads import stats
from apps.ads.media import MediaError, save_upload
from apps.ads.models import (DEFAULT_TABLE_COLUMNS, TABLE_COLUMNS, Ad, AdDraft, AdMedia, AdPricing, AdRevision,
                             AdStateError, AdStatus, AdTableSettings)
from apps.ads.serializers import (AdBudgetTopUpSerializer, AdCreateSerializer, AdDraftSerializer, AdMediaSerializer,
                                  AdPricingSerializer, AdSerializer, AdTableSettingsSerializer, AdUpdateSerializer,
                                  BudgetOperationSerializer)
from apps.ads.serving.store import ServingUnavailable
from apps.billing.models import MoneyOperation
from apps.billing.services import charge, format_money, refund_wallet_amount
from apps.common.pagination import StandardResultsSetPagination
from apps.common.schema import ERROR_RESPONSE, ORDER_CREATE_ERROR_RESPONSES

# Ответ на недопустимое действие: {"error": "..."} или {"<поле>": "..."}
STATE_ERROR_RESPONSE = {'type': 'object', 'additionalProperties': {'type': 'string'}}
DELETE_RESPONSE = {
    'type': 'object',
    'properties': {
        'message': {'type': 'string'},
        'refund_amount': {'type': 'string', 'format': 'decimal', 'description': 'Точный остаток из объявления'},
        'refund_wallet_amount': {'type': 'string', 'format': 'decimal',
                                 'description': 'Сколько реально пришло на баланс (в AdGram — целые сумы, '
                                                'дробь копится в carry). Это число показывать в тосте'},
    },
}
# Redis выдачи недоступен: без него нельзя записать уже показанное — деньги не двигаем
SERVING_DOWN = 'Сервис выдачи рекламы временно недоступен, попробуйте позже'


def own_ads(user):
    """Свои объявления владельца; удалённые в API не видны"""
    return Ad.objects.filter(user=user).exclude(status=AdStatus.DELETED)


def state_error(exc):
    return Response({exc.field or 'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)


ALL_VIEWS = F('views_total') + F('free_views_total')
# ?ordering= : столбец таблицы → выражение (null всегда в конце). targeting не сортируется
ORDERING = {
    'created_at': F('created_at'),
    'title': Lower('title'),
    'url': F('url'),
    'status': F('status'),
    'cpm': F('cpm'),
    'spent': F('spent'),
    'remaining_budget': F('budget') - F('spent') - F('refunded'),
    'views': ALL_VIEWS,
    'opened_video': F('video_opens_total'),
    'clicks': F('clicks_total'),
    'actions': F('actions_total'),
    'action_type': Case(When(promoted_peer__peer_type='BOT', then=Value('START')),
                        When(promoted_peer__peer_type='USER', then=Value('MESSAGE')),
                        When(promoted_peer__isnull=True, url__iendswith='bot', then=Value('START')),
                        default=Value('JOIN')),
    'ctr': Cast('clicks_total', FloatField()) / NullIf(ALL_VIEWS, 0),
    'cvr': Cast('actions_total', FloatField()) / NullIf(F('clicks_total'), 0),
    'cpc': Cast(F('spent') / NullIf(F('clicks_total'), 0), DecimalField(max_digits=30, decimal_places=6)),
    'cpa': Cast(F('spent') / NullIf(F('actions_total'), 0), DecimalField(max_digits=30, decimal_places=6)),
}
ORDERING_HELP = (f'Столбец: {", ".join(ORDERING)}; «-» впереди — по убыванию. По умолчанию -created_at. '
                 f'Пустые значения (деление на ноль) — в конце. targeting не сортируется')


class AdListCreateView(generics.ListCreateAPIView):
    """
    Список своих объявлений и создание нового. Поиск ?q= — по названию, ссылке и username
    продвигаемого; сортировка ?ordering= по любому столбцу таблицы (кроме targeting)
    """
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = StandardResultsSetPagination
    serializer_class = AdSerializer

    @extend_schema(parameters=[
        OpenApiParameter('q', str, description='Поиск по названию, ссылке или username продвигаемого'),
        OpenApiParameter('ordering', str, description=ORDERING_HELP),
    ])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return Ad.objects.none()
        # Канал, медиа и правки — заранее, без запросов на каждую строку
        queryset = own_ads(self.request.user).select_related('promoted_peer', 'media').prefetch_related(
            Prefetch('revisions', queryset=AdRevision.objects.select_related('media').order_by('-created_at'),
                     to_attr='listed_revisions'))
        query = self.request.query_params.get('q', '').strip()
        if query:
            queryset = queryset.filter(Q(title__icontains=query) | Q(url__icontains=query)
                                       | Q(promoted_peer__username__icontains=query.lstrip('@')))
        return self._ordered(queryset)

    def _ordered(self, queryset):
        ordering = self.request.query_params.get('ordering') or '-created_at'
        column = ordering.removeprefix('-')
        if column not in ORDERING:
            raise serializers.ValidationError({'ordering': [ORDERING_HELP]})
        value = F('sort_value')
        sort = value.desc(nulls_last=True) if ordering.startswith('-') else value.asc(nulls_last=True)
        return queryset.annotate(sort_value=ORDERING[column]).order_by(sort, '-created_at', 'pk')

    @extend_schema(request=AdCreateSerializer, responses={201: AdSerializer, **ORDER_CREATE_ERROR_RESPONSES})
    def post(self, request, *args, **kwargs):
        serializer = AdCreateSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        ad = serializer.save()
        return Response(AdSerializer(ad, context={'request': request}).data, status=status.HTTP_201_CREATED)


class AdDetailView(generics.GenericAPIView):
    """Детали, правка без модерации (PATCH) и мягкое удаление с возвратом остатка"""
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = AdSerializer

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return Ad.objects.none()
        return own_ads(self.request.user)

    def get(self, request, pk):
        return Response(AdSerializer(self.get_object(), context={'request': request}).data)

    @extend_schema(request=AdUpdateSerializer, responses={
        200: AdSerializer,  # всё применено сразу
        202: AdSerializer,  # креатив ушёл на модерацию — смотри pending_revision
        400: STATE_ERROR_RESPONSE,
    })
    def patch(self, request, pk):
        serializer = AdUpdateSerializer(instance=self.get_object(), data=request.data, partial=True,
                                        context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        settings_changes, creative_changes = serializer.split()
        revision = None
        try:
            with transaction.atomic():
                ad = get_object_or_404(self.get_queryset().select_for_update(), pk=pk)
                ad.update_settings(settings_changes)
                if creative_changes:
                    # CPM прислан в том же запросе — владелец сам назначил цену под новый креатив
                    revision = ad.update_creative(creative_changes, request.user,
                                                  reprice='cpm' not in settings_changes)
        except AdStateError as exc:
            return state_error(exc)
        return Response(AdSerializer(ad, context={'request': request}).data,
                        status=status.HTTP_202_ACCEPTED if revision else status.HTTP_200_OK)

    @extend_schema(responses={200: DELETE_RESPONSE, 503: ERROR_RESPONSE})
    def delete(self, request, pk):
        try:
            with transaction.atomic():
                ad = get_object_or_404(self.get_queryset().select_for_update(), pk=pk)
                refund_amount = ad.soft_delete()
                wallet_amount = refund_wallet_amount(ad, MoneyOperation.Reason.DELETE, refund_amount)
        except ServingUnavailable:
            return Response({'error': SERVING_DOWN}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({
            'message': 'Объявление удалено, остаток бюджета возвращён',
            'refund_amount': format_money(refund_amount),
            'refund_wallet_amount': format_money(wallet_amount),
        })


class AdBudgetTopUpView(generics.GenericAPIView):
    """Пополнение бюджета (с кошелька AdGram или локального баланса). STOPPED снова станет ACTIVE"""
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = AdBudgetTopUpSerializer

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return Ad.objects.none()
        return own_ads(self.request.user)

    @extend_schema(responses={200: AdSerializer, **ORDER_CREATE_ERROR_RESPONSES})
    def post(self, request, pk):
        ad = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Проверяем статус ДО списания денег (под локом проверит ещё раз add_budget)
        if ad.status not in Ad.BUDGET_STATUSES:
            return Response({'error': f'Нельзя пополнить бюджет: объявление в статусе «{ad.get_status_display()}»'},
                            status=status.HTTP_400_BAD_REQUEST)

        def lock_ad():
            return Ad.objects.select_for_update().get(pk=ad.pk)

        def add_to_ad(amount):
            # amount = оплачено + дробный остаток прошлых возвратов (его добавляет billing).
            # Объявление уже под локом с начала транзакции (lock_order) — повторный лок не ждёт
            locked = lock_ad()
            locked.add_budget(amount)
            return locked

        try:
            # Сначала объявление, потом баланс — как у удаления и блокировки (иначе deadlock)
            ad = charge(request.user, serializer.validated_data['amount'], MoneyOperation.OrderType.ADS,
                        add_to_ad, reason=MoneyOperation.Reason.TOP_UP, lock_order=lock_ad)
        except AdStateError as exc:
            # Статус успел смениться после проверки: у локального кошелька всё откатилось,
            # у AdGram списание без объявления вернёт компенсация (через 10 минут)
            return state_error(exc)
        except ServingUnavailable:
            # Так же: локально откатилось, списание AdGram вернёт компенсация
            return Response({'detail': SERVING_DOWN}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(AdSerializer(ad, context={'request': request}).data)


class AdTableSettingsView(APIView):
    """Какие столбцы таблицы объявлений показывать (как «Customize Table» у Telegram — хранится на сервере)"""
    permission_classes = [permissions.IsAuthenticated]

    def payload(self, columns):
        return {'columns': columns, 'available': list(TABLE_COLUMNS), 'default': list(DEFAULT_TABLE_COLUMNS)}

    @extend_schema(responses={200: AdTableSettingsSerializer})
    def get(self, request):
        return Response(self.payload(AdTableSettings.columns_for(request.user)))

    @extend_schema(request=AdTableSettingsSerializer, responses={200: AdTableSettingsSerializer})
    def put(self, request):
        serializer = AdTableSettingsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        columns = serializer.validated_data['columns']
        AdTableSettings.objects.update_or_create(user=request.user, defaults={'columns': columns})
        return Response(self.payload(columns))


def draft_payload(draft):
    if draft is None:
        return {'data': None, 'updated_at': None}
    return AdDraftSerializer(draft).data


class DraftThrottle(UserRateThrottle):
    scope = 'ads_draft'  # 60 в минуту на пользователя (settings.DEFAULT_THROTTLE_RATES)


class AdDraftView(APIView):
    """Черновик формы создания (один на пользователя, как у Telegram): фронт сохраняет через 3 с после изменений"""
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [DraftThrottle]

    @extend_schema(responses={200: AdDraftSerializer})
    def get(self, request):
        return Response(draft_payload(AdDraft.objects.filter(user=request.user).first()))

    @extend_schema(request=AdDraftSerializer, responses={200: AdDraftSerializer, 400: STATE_ERROR_RESPONSE})
    def put(self, request):
        serializer = AdDraftSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        draft, _ = AdDraft.objects.update_or_create(user=request.user,
                                                    defaults={'data': serializer.validated_data['data']})
        return Response(draft_payload(draft))

    @extend_schema(responses={204: None})
    def delete(self, request):
        """Clear Draft (черновика нет — тоже 204)"""
        AdDraft.objects.filter(user=request.user).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdCopyToDraftView(generics.GenericAPIView):
    """
    «Create a similar ad»: поля объявления → черновик формы создания (прежний черновик заменяется).
    Не копируются деньги (budget), статусы, даты и согласие с правилами
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = AdDraftSerializer

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return Ad.objects.none()
        return own_ads(self.request.user).select_related('promoted_peer')

    @extend_schema(request=None, responses={200: AdDraftSerializer, 404: ERROR_RESPONSE})
    def post(self, request, pk):
        return Response(draft_payload(AdDraft.copy_from(self.get_object())))


STATS_DAY = {
    'type': 'object',
    'properties': {
        'date': {'type': 'string', 'format': 'date'},
        'views': {'type': 'integer', 'description': 'Платные показы'},
        'free_views': {'type': 'integer', 'description': 'Бесплатные (кончился бюджет / лимит, объявление не активно)'},
        'clicks': {'type': 'integer'},
        'actions': {'type': 'integer'},
        'video_opens': {'type': 'integer'},
        'spent': {'type': 'string', 'format': 'decimal'},
    },
}
STATS_RESPONSE = {
    'type': 'object',
    'properties': {
        'from': {'type': 'string', 'format': 'date'},
        'to': {'type': 'string', 'format': 'date'},
        'days': {'type': 'array', 'items': STATS_DAY, 'description': 'Каждый день периода, дни без показов — нули'},
        'totals': {
            'type': 'object',
            'properties': {
                **{key: value for key, value in STATS_DAY['properties'].items() if key != 'date'},
                'ctr': {'type': 'number', 'nullable': True, 'description': 'clicks / (views + free_views)'},
                'cvr': {'type': 'number', 'nullable': True, 'description': 'actions / clicks'},
                'cpc': {'type': 'string', 'format': 'decimal', 'nullable': True},
                'cpa': {'type': 'string', 'format': 'decimal', 'nullable': True},
            },
        },
    },
}
MAX_STATS_PERIOD = timedelta(days=365)


class AdStatsView(generics.GenericAPIView):
    """
    Статистика объявления по дням (сутки по Ташкенту) за период ?from=&to= (YYYY-MM-DD, включительно;
    по умолчанию — последние 30 дней, не больше года). Показы последних секунд могут прийти через 5 с
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = AdSerializer

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return Ad.objects.none()
        return own_ads(self.request.user)

    @extend_schema(parameters=[
        OpenApiParameter('from', str, description='Первый день, YYYY-MM-DD'),
        OpenApiParameter('to', str, description='Последний день, YYYY-MM-DD (по умолчанию — сегодня)'),
    ], responses={200: STATS_RESPONSE, 400: STATE_ERROR_RESPONSE})
    def get(self, request, pk):
        ad = self.get_object()
        try:
            end = date.fromisoformat(request.query_params['to']) if request.query_params.get('to') \
                else timezone.localdate()
            start = date.fromisoformat(request.query_params['from']) if request.query_params.get('from') \
                else end - timedelta(days=29)
        except ValueError:
            return Response({'error': 'Даты — в формате YYYY-MM-DD'}, status=status.HTTP_400_BAD_REQUEST)
        if start > end:
            return Response({'error': 'Начало периода позже конца'}, status=status.HTTP_400_BAD_REQUEST)
        if end - start > MAX_STATS_PERIOD:
            return Response({'error': 'Период — не больше года'}, status=status.HTTP_400_BAD_REQUEST)
        return Response(stats.period(ad, start, end))


class AdBudgetHistoryView(generics.ListAPIView):
    """История операций бюджета объявления (из журнала MoneyOperation), новые сверху"""
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = BudgetOperationSerializer
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return MoneyOperation.objects.none()
        ad = get_object_or_404(own_ads(self.request.user), pk=self.kwargs['pk'])
        return MoneyOperation.objects.filter(
            order_type=MoneyOperation.OrderType.ADS, order_id=ad.pk,
        ).order_by('-created_at')


class AdPricingView(APIView):
    """Минимальные CPM, наценки, минимальный бюджет и версия правил — для формы создания"""
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: AdPricingSerializer})
    def get(self, request):
        return Response(AdPricingSerializer.from_pricing(AdPricing.load()).data)


logger = logging.getLogger(__name__)
MEDIA_UPLOAD_REQUEST = {'multipart/form-data': {'type': 'object', 'properties': {
    'file': {'type': 'string', 'format': 'binary',
             'description': 'Фото jpg/png (до 5 МБ) или видео mp4/mov (до 20 МБ, 3–60 с); 16:9, ширина от 640 px'},
}, 'required': ['file']}}


def enqueue_video(media_id):
    """Ставит видео в обработку. Брокер недоступен — сразу FAILED, а не вечный PROCESSING"""
    from apps.ads.tasks import process_ad_media
    try:
        process_ad_media.delay(str(media_id))
    except Exception:
        logger.exception('Не удалось поставить видео %s в обработку', media_id)
        AdMedia.objects.filter(pk=media_id, status=AdMedia.Status.PROCESSING).update(
            status=AdMedia.Status.FAILED, error='Не удалось поставить видео в обработку, загрузите ещё раз')


class AdMediaUploadView(generics.GenericAPIView):
    """
    Загрузка фото или видео для объявления (до создания объявления или правки).
    Проверки — по содержимому файла. Фото сразу READY; видео — PROCESSING, пока перекодируется
    в фоне (статус — GET /api/ads/media/<id>/). Прикрепить можно только READY.
    """
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]
    serializer_class = AdMediaSerializer

    @extend_schema(request=MEDIA_UPLOAD_REQUEST, responses={201: AdMediaSerializer, 400: STATE_ERROR_RESPONSE})
    def post(self, request):
        uploaded = request.FILES.get('file')
        if uploaded is None:
            return Response({'file': 'Прикрепите файл'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            media = save_upload(request.user, uploaded)
        except MediaError as exc:
            return Response({'file': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if media.status == AdMedia.Status.PROCESSING:
            transaction.on_commit(lambda: enqueue_video(media.pk))
        return Response(AdMediaSerializer(media, context={'request': request}).data, status=status.HTTP_201_CREATED)


class AdMediaDetailView(generics.RetrieveAPIView):
    """Статус медиа (фронт опрашивает, пока видео PROCESSING). Чужое — 404"""
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = AdMediaSerializer

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return AdMedia.objects.none()
        return AdMedia.objects.filter(user=self.request.user)

