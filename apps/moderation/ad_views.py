"""
Модерация объявлений v2 (apps.ads): очередь, все объявления, карточка, решения
по объявлениям и по правкам креатива. Деньги двигают методы модели (refund из billing).
"""
from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.ads.models import Ad, AdRevision, AdStateError, AdStatus, Placement
from apps.billing.services import format_money
from apps.common.pagination import StandardResultsSetPagination
from apps.common.permissions import IsModerator
from apps.common.schema import ERROR_RESPONSE

from .ad_serializers import ModerationAdDetailSerializer, ModerationAdSerializer, ModerationRevisionSerializer
from .serializers import ModerationReasonSerializer

DECISION_ERROR_RESPONSES = {
    403: ERROR_RESPONSE,  # своё объявление модерировать нельзя
    404: ERROR_RESPONSE,
    409: ERROR_RESPONSE,  # уже не в том статусе (решили раньше / правку заменили)
}
AD_DECISION_RESPONSE = {
    'type': 'object',
    'properties': {
        'message': {'type': 'string'},
        'ad_id': {'type': 'string', 'format': 'uuid'},
        'status': {'type': 'string'},
        'refund_amount': {'type': 'string', 'format': 'decimal', 'description': 'Только у decline и block'},
    },
}
REVISION_DECISION_RESPONSE = {
    'type': 'object',
    'properties': {
        'message': {'type': 'string'},
        'revision_id': {'type': 'string', 'format': 'uuid'},
        'ad_id': {'type': 'string', 'format': 'uuid'},
        'status': {'type': 'string'},
    },
}


def is_own(user, ad):
    """Модератор не модерирует свои объявления (суперадмин может — как у старых заказов)"""
    return ad.user_id == user.id and not user.is_superuser


OWN_AD_RESPONSE = {'error': 'Нельзя модерировать собственное объявление'}


class PendingAdsListView(generics.ListAPIView):
    """Очередь: объявления на модерации, старые сверху"""
    serializer_class = ModerationAdSerializer
    permission_classes = [IsModerator]
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        return Ad.objects.filter(status=AdStatus.IN_REVIEW).select_related('user').order_by('created_at')


class AllAdsListView(generics.ListAPIView):
    """Все объявления (кроме удалённых), новые сверху. Фильтры: ?status=, ?placement="""
    serializer_class = ModerationAdSerializer
    permission_classes = [IsModerator]
    pagination_class = StandardResultsSetPagination

    @extend_schema(parameters=[
        OpenApiParameter('status', str, description='Статус объявления (IN_REVIEW, ACTIVE, ON_HOLD, ...)'),
        OpenApiParameter('placement', str, description='Место показа (SEARCH, BOT, ...)'),
    ])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        queryset = (Ad.objects.exclude(status=AdStatus.DELETED)
                    .select_related('user').order_by('-created_at'))
        params = self.request.query_params
        if params.get('status') in AdStatus.values:
            queryset = queryset.filter(status=params['status'])
        if params.get('placement') in Placement.values:
            queryset = queryset.filter(placement=params['placement'])
        return queryset


class ModerationAdDetailView(generics.RetrieveAPIView):
    """Полная карточка: креатив, таргетинг, владелец, бюджет, правки и история решений"""
    serializer_class = ModerationAdDetailSerializer
    permission_classes = [IsModerator]
    # Удалённые тоже открываются — чтобы ссылка из уведомления не вела в 404
    queryset = Ad.objects.select_related('user').prefetch_related('revisions', 'decisions__moderator')


class BaseAdDecisionView(APIView):
    """Общий код решения по объявлению; действие (approve/decline/block) задаёт наследник"""
    permission_classes = [IsModerator]
    action = None

    def post(self, request, pk):
        reason = ''
        if self.action != 'approve':
            serializer = ModerationReasonSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            reason = serializer.validated_data['reason'].strip()

        refund_amount = None
        try:
            with transaction.atomic():
                ad = get_object_or_404(Ad.objects.select_for_update(), pk=pk)
                if is_own(request.user, ad):
                    return Response(OWN_AD_RESPONSE, status=status.HTTP_403_FORBIDDEN)
                if self.action == 'approve':
                    ad.approve(request.user)
                else:
                    refund_amount = getattr(ad, self.action)(request.user, reason)
        except AdStateError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_409_CONFLICT)

        messages = {
            'approve': 'Объявление одобрено',
            'decline': 'Объявление отклонено, деньги возвращены владельцу',
            'block': 'Объявление заблокировано, остаток бюджета возвращён владельцу',
        }
        data = {'message': messages[self.action], 'ad_id': str(ad.pk), 'status': ad.status}
        if refund_amount is not None:
            data['refund_amount'] = format_money(refund_amount)
        return Response(data)


@extend_schema_view(post=extend_schema(request=None, responses={200: AD_DECISION_RESPONSE,
                                                                **DECISION_ERROR_RESPONSES}))
class ApproveAdView(BaseAdDecisionView):
    """Одобрить (из IN_REVIEW) — объявление переходит в статус, выбранный при создании"""
    action = 'approve'


@extend_schema_view(post=extend_schema(request=ModerationReasonSerializer,
                                       responses={200: AD_DECISION_RESPONSE, **DECISION_ERROR_RESPONSES}))
class DeclineAdView(BaseAdDecisionView):
    """Отклонить (из IN_REVIEW) — весь остаток бюджета сразу возвращается. Причина обязательна"""
    action = 'decline'


@extend_schema_view(post=extend_schema(request=ModerationReasonSerializer,
                                       responses={200: AD_DECISION_RESPONSE, **DECISION_ERROR_RESPONSES}))
class BlockAdView(BaseAdDecisionView):
    """Заблокировать за нарушение — остаток возвращается, правка на проверке гасится. Причина обязательна"""
    action = 'block'


class PendingRevisionsListView(generics.ListAPIView):
    """Правки креатива на проверке, старые сверху: текущая и новая версии рядом"""
    serializer_class = ModerationRevisionSerializer
    permission_classes = [IsModerator]
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        return (AdRevision.objects.filter(status=AdRevision.Status.PENDING)
                .select_related('ad', 'ad__user').order_by('created_at'))


class BaseRevisionDecisionView(APIView):
    """Решение по правке креатива; деньги не двигаются"""
    permission_classes = [IsModerator]
    action = None

    def post(self, request, pk):
        reason = ''
        if self.action == 'decline':
            serializer = ModerationReasonSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            reason = serializer.validated_data['reason'].strip()

        try:
            with transaction.atomic():
                try:
                    ad, revision = AdRevision.lock_with_ad(pk)
                except AdRevision.DoesNotExist:
                    return Response({'error': 'Правка не найдена'}, status=status.HTTP_404_NOT_FOUND)
                if is_own(request.user, ad):
                    return Response(OWN_AD_RESPONSE, status=status.HTTP_403_FORBIDDEN)
                if self.action == 'approve':
                    revision.approve(ad, request.user)
                else:
                    revision.decline(ad, request.user, reason)
        except AdStateError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_409_CONFLICT)

        message = 'Правка одобрена и уже показывается' if self.action == 'approve' else 'Правка отклонена'
        return Response({'message': message, 'revision_id': str(revision.pk), 'ad_id': str(ad.pk),
                         'status': revision.status})


@extend_schema_view(post=extend_schema(request=None, responses={200: REVISION_DECISION_RESPONSE,
                                                                **DECISION_ERROR_RESPONSES}))
class ApproveRevisionView(BaseRevisionDecisionView):
    """Одобрить правку — креатив переносится в объявление, статус объявления не меняется"""
    action = 'approve'


@extend_schema_view(post=extend_schema(request=ModerationReasonSerializer,
                                       responses={200: REVISION_DECISION_RESPONSE, **DECISION_ERROR_RESPONSES}))
class DeclineRevisionView(BaseRevisionDecisionView):
    """Отклонить правку — объявление остаётся в прежнем виде. Причина обязательна"""
    action = 'decline'
