import json
from decimal import Decimal

from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.ads import stats, validators
from apps.ads.models import (MEDIA_PLACEMENTS, TABLE_COLUMNS, ActionType, Ad, AdDraft, AdMedia, AdPricing,
                             AdRevision, AdStateError, InitialStatus, Placement, default_schedule)
from apps.billing.models import MoneyOperation
from apps.billing.services import charge, check_budget, format_money
from apps.directories.serializers import TelegramPeerSerializer
from apps.directories.telegram import PeerBatch


@extend_schema_field({'type': 'string', 'format': 'decimal', 'example': '10000.00'})
class MoneyField(serializers.Field):
    """Деньги строкой: минимум 2 знака, лишние нули справа убираем (точность не теряем)"""

    def __init__(self, **kwargs):
        kwargs['read_only'] = True
        super().__init__(**kwargs)

    def to_representation(self, value):
        return format_money(value)


class AdMediaBriefSerializer(serializers.ModelSerializer):
    """Медиа внутри объявления, правки и карточки модератора"""
    type = serializers.CharField(source='media_type', read_only=True)
    url = serializers.SerializerMethodField()

    class Meta:
        model = AdMedia
        fields = ['id', 'type', 'url', 'width', 'height', 'duration']
        read_only_fields = fields

    def get_url(self, obj) -> str:
        request = self.context.get('request')
        return request.build_absolute_uri(obj.file.url) if request else obj.file.url


class AdMediaSerializer(AdMediaBriefSerializer):
    """Загруженное медиа и его статус (фронт опрашивает, пока видео обрабатывается)"""

    class Meta(AdMediaBriefSerializer.Meta):
        fields = AdMediaBriefSerializer.Meta.fields + ['status', 'error', 'size', 'created_at']
        read_only_fields = fields


class AdRevisionSerializer(serializers.ModelSerializer):
    """Правка креатива (ревизия)"""
    media = AdMediaBriefSerializer(read_only=True, allow_null=True)

    class Meta:
        model = AdRevision
        fields = ['id', 'text', 'show_channel_picture', 'other_info', 'media', 'status', 'reason',
                  'created_at', 'moderated_at']
        read_only_fields = fields


def resolve_media(user, media_id, placement):
    """Медиа для объявления: только своё, готовое и только у сообщения в канале"""
    if media_id is None:
        return None
    if placement not in MEDIA_PLACEMENTS:
        raise serializers.ValidationError('Фото и видео — только у сообщения в канале')
    media = AdMedia.objects.filter(pk=media_id, user=user).first()
    if media is None:
        raise serializers.ValidationError('Медиа не найдено')  # чужое — тоже «не найдено»
    if media.status == AdMedia.Status.PROCESSING:
        raise serializers.ValidationError('Видео ещё обрабатывается — дождитесь статуса READY')
    if media.status == AdMedia.Status.FAILED:
        raise serializers.ValidationError(f'Медиа не прошло проверку: {media.error}')
    return media


ERR_MEDIA_WITH_PICTURE = 'Фото канала не показывается вместе с медиа — уберите одно из двух'


TARGETING_SUMMARY = {
    'type': 'object',
    'properties': {
        'kind': {'type': 'string', 'enum': ['queries', 'bots', 'all_bots', 'channels', 'languages', 'locations',
                                            'countries']},
        'count': {'type': 'integer', 'nullable': True, 'description': 'null у all_bots (показ в любых ботах)'},
    },
}


class AdSerializer(serializers.ModelSerializer):
    """Объявление глазами владельца (список и детали): + показы, клики, действия и метрики, как у Telegram"""
    ad_format = serializers.CharField(read_only=True, help_text='search / bot / users / channels')
    budget = MoneyField(help_text='Всего внесено в бюджет')
    spent = MoneyField()
    refunded = MoneyField()
    remaining_budget = MoneyField(help_text='Остаток: budget − spent − refunded')
    views = serializers.SerializerMethodField(help_text='Все показы: платные + бесплатные')
    opened_video = serializers.IntegerField(source='video_opens_total', read_only=True)
    clicks = serializers.IntegerField(source='clicks_total', read_only=True)
    actions = serializers.IntegerField(source='actions_total', read_only=True)
    action_type = serializers.ChoiceField(choices=ActionType.choices, read_only=True,
                                          help_text='JOIN — подписка на канал, START — запуск бота')
    ctr = serializers.SerializerMethodField(help_text='clicks / views, доля (6 знаков); null — показов нет')
    cvr = serializers.SerializerMethodField(help_text='actions / clicks, доля; null — кликов нет')
    cpc = serializers.SerializerMethodField(help_text='spent / clicks; null — кликов нет')
    cpa = serializers.SerializerMethodField(help_text='spent / actions; null — действий нет')
    targeting_summary = serializers.SerializerMethodField()
    promoted_peer = TelegramPeerSerializer(read_only=True, help_text='Продвигаемый канал или бот (для превью)')
    media = AdMediaBriefSerializer(read_only=True, allow_null=True)
    pending_revision = serializers.SerializerMethodField(
        help_text='Правка креатива на проверке (пока её не одобрят, крутится прежний вариант)')
    last_declined_revision = serializers.SerializerMethodField(
        help_text='Последняя правка, если её отклонили (с причиной); после новой правки — null')

    class Meta:
        model = Ad
        fields = [
            'id', 'placement', 'ad_format', 'title', 'text', 'url', 'promoted_peer', 'show_channel_picture',
            'other_info', 'media',
            'cpm', 'budget', 'spent', 'refunded', 'remaining_budget', 'daily_limit', 'views_per_user_per_day',
            'status', 'initial_status', 'start_date', 'end_date', 'schedule', 'targeting', 'targeting_summary',
            'views', 'opened_video', 'clicks', 'actions', 'action_type', 'ctr', 'cvr', 'cpc', 'cpa',
            'moderation_reason', 'pending_revision', 'last_declined_revision',
            'terms_version', 'terms_accepted_at', 'created_at', 'updated_at',
        ]
        read_only_fields = fields

    def get_views(self, obj) -> int:
        return obj.views_total + obj.free_views_total

    def get_ctr(self, obj) -> float | None:
        return stats.ratio(obj.clicks_total, self.get_views(obj))

    def get_cvr(self, obj) -> float | None:
        return stats.ratio(obj.actions_total, obj.clicks_total)

    @extend_schema_field({'type': 'string', 'format': 'decimal', 'nullable': True})
    def get_cpc(self, obj):
        return stats.money_per(obj.spent, obj.clicks_total)

    @extend_schema_field({'type': 'string', 'format': 'decimal', 'nullable': True})
    def get_cpa(self, obj):
        return stats.money_per(obj.spent, obj.actions_total)

    @extend_schema_field(TARGETING_SUMMARY)
    def get_targeting_summary(self, obj):
        return stats.targeting_summary(obj)

    @extend_schema_field(AdRevisionSerializer(allow_null=True))
    def get_pending_revision(self, obj):
        listed = getattr(obj, 'listed_revisions', None)  # список заранее подгружает одним запросом
        if listed is not None:
            revision = next((item for item in listed if item.status == AdRevision.Status.PENDING), None)
        else:
            revision = obj.pending_revision()
        return AdRevisionSerializer(revision, context=self.context).data if revision else None

    @extend_schema_field(AdRevisionSerializer(allow_null=True))
    def get_last_declined_revision(self, obj):
        listed = getattr(obj, 'listed_revisions', None)  # новые сверху
        if listed is not None:
            revision = listed[0] if listed and listed[0].status == AdRevision.Status.DECLINED else None
        else:
            revision = obj.last_declined_revision()
        return AdRevisionSerializer(revision, context=self.context).data if revision else None


class AdCreateSerializer(serializers.Serializer):
    """Создание объявления: проверки → списание (billing.charge) → статус IN_REVIEW"""
    placement = serializers.ChoiceField(choices=Placement.choices)
    title = serializers.CharField(max_length=255)
    text = serializers.CharField(required=False, allow_blank=True, default='',
                                 help_text='До 160 символов, без переносов строк. Для SEARCH — пусто')
    url = serializers.CharField(max_length=200, help_text='t.me/<username>')
    show_channel_picture = serializers.BooleanField(required=False, default=False)
    other_info = serializers.CharField(required=False, allow_blank=True, default='', max_length=255)
    media = serializers.UUIDField(required=False, allow_null=True, default=None,
                                  help_text='id загруженного медиа (READY); только у сообщения в канале')
    cpm = serializers.IntegerField(min_value=1, help_text='Цена 1000 показов, целые сумы')
    budget = serializers.DecimalField(max_digits=19, decimal_places=2, min_value=Decimal('0.01'),
                                      help_text='У пользователя AdGram — целые сумы')
    daily_limit = serializers.IntegerField(required=False, allow_null=True, default=None, min_value=1)
    views_per_user_per_day = serializers.IntegerField(required=False, default=1, min_value=0, max_value=4,
                                                      help_text='0 — без ограничений')
    initial_status = serializers.ChoiceField(choices=InitialStatus.choices, required=False,
                                             default=InitialStatus.ACTIVE)
    start_date = serializers.DateTimeField(required=False, allow_null=True, default=None)
    end_date = serializers.DateTimeField(required=False, allow_null=True, default=None)
    schedule = serializers.JSONField(required=False, default=default_schedule,
                                     help_text='{enabled, days: 7 масок по 24 бита (Пн…Вс), timezone}')
    targeting = serializers.JSONField(required=False, default=dict, help_text='Поля зависят от placement')
    agree_terms = serializers.BooleanField(help_text='Согласие с правилами площадки — обязательно true')

    def validate_agree_terms(self, value):
        if value is not True:
            raise serializers.ValidationError('Нужно согласиться с правилами площадки')
        return value

    def validate_budget(self, value):
        return check_budget(self.context['request'].user, value)

    def validate_start_date(self, value):
        return validators.check_date(value)

    def validate_end_date(self, value):
        return validators.check_date(value)

    def validate(self, data):
        pricing = AdPricing.load()
        placement = data['placement']
        # Суперадмин (свои кампании, перенос старых заказов): без минимального CPM и лимита в 10 фраз
        superadmin = self.context['request'].user.is_superuser
        errors = {}

        def check(field, func):
            try:
                return func()
            except serializers.ValidationError as exc:
                errors[field] = exc.detail

        data['text'] = check('text', lambda: validators.check_text(placement, data['text']))
        # Каналы и боты проверяем в Telegram: свежие — из кэша, новые — по очереди, с общим лимитом
        # времени (20 с) и числа новых имён (20) на весь запрос — и для ссылки, и для таргетинга
        batch = PeerBatch()
        username = check('url', lambda: validators.parse_promoted_url(data['url']))
        promoted_peer = None
        if username:
            data['url'] = validators.promoted_url(username)
            promoted_peer = check('url', lambda: validators.check_promoted_peer(username, batch.resolve))
        data['promoted_peer'] = promoted_peer
        if placement == Placement.SEARCH and data['show_channel_picture']:
            errors['show_channel_picture'] = 'В рекламе в поиске фото канала нет'
        data['schedule'] = check('schedule', lambda: validators.check_schedule(placement, data['schedule']))
        data['targeting'] = check(
            'targeting', lambda: validators.validate_targeting(placement, data['targeting'], promoted_peer,
                                                               batch.resolve, superadmin=superadmin))

        data['media'] = check('media', lambda: resolve_media(
            self.context['request'].user, data['media'], placement))
        if data['media'] and data['show_channel_picture']:
            errors['media'] = ERR_MEDIA_WITH_PICTURE

        # Одна наценка к минимуму: видео / фото (дороже при выборе конкретных каналов) / фото канала.
        # У суперадмина минимума нет — любой CPM от 1 сума (как spm старых заказов)
        min_cpm = pricing.min_cpm_for(placement, data['targeting'], data['show_channel_picture'], data['media'])
        if not superadmin and data['cpm'] < min_cpm:
            errors['cpm'] = f"CPM can't be less than {min_cpm}"
        if data['budget'] < pricing.min_budget:
            errors['budget'] = f'Минимальный бюджет — {pricing.min_budget} сум'

        # Правила дат те же, что у модели (старт — только для ON_HOLD, конец позже старта)
        draft = Ad(initial_status=data['initial_status'], start_date=data['start_date'], end_date=data['end_date'])
        try:
            draft.check_dates()
        except AdStateError as exc:
            errors[exc.field] = str(exc)

        if errors:
            raise serializers.ValidationError(errors)
        self._pricing = pricing
        return data

    def create(self, validated_data):
        user = self.context['request'].user
        pricing = self._pricing
        paid = validated_data.pop('budget')
        validated_data.pop('agree_terms')

        def create_ad(budget):
            # budget = оплачено + дробный остаток прошлых возвратов (его добавляет billing)
            ad = Ad(user=user, budget=budget, terms_version=pricing.terms_version,
                    terms_accepted_at=timezone.now(), **validated_data)
            ad.submit()  # на модерацию (у суперадмина — сразу одобрено)
            AdDraft.objects.filter(user=user).delete()  # объявление создано — черновик больше не нужен
            return ad

        return charge(user, paid, MoneyOperation.OrderType.ADS, create_ad)


# Что владелец может менять: сразу (настройки и title) или через модерацию (креатив).
# Остальное отдаёт 400 с причиной
SETTINGS_FIELDS = {'title', 'cpm', 'daily_limit', 'views_per_user_per_day', 'status', 'start_date', 'end_date',
                   'schedule'}
EDITABLE_FIELDS = SETTINGS_FIELDS | set(AdRevision.CREATIVE_FIELDS)
FORBIDDEN_FIELD_MESSAGES = {
    'targeting': 'Таргетинг после создания не меняется',
    'placement': 'Место показа после создания не меняется',
    'url': 'Ссылка после создания не меняется',
    'budget': 'Бюджет пополняется через POST /api/ads/<id>/budget/',
}


class AdUpdateSerializer(serializers.Serializer):
    """
    PATCH владельца. Настройки и title меняются сразу; креатив (text, show_channel_picture,
    other_info) — через ревизию на модерации (если объявление уже одобрено).
    """
    title = serializers.CharField(required=False, max_length=255)
    text = serializers.CharField(required=False, allow_blank=True)
    show_channel_picture = serializers.BooleanField(required=False)
    other_info = serializers.CharField(required=False, allow_blank=True, max_length=255)
    media = serializers.UUIDField(required=False, allow_null=True, help_text='id медиа (READY) или null — убрать')
    cpm = serializers.IntegerField(required=False, min_value=1)
    daily_limit = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    views_per_user_per_day = serializers.IntegerField(required=False, min_value=0, max_value=4)
    status = serializers.ChoiceField(choices=InitialStatus.choices, required=False,
                                     help_text='Только ACTIVE ↔ ON_HOLD после одобрения')
    start_date = serializers.DateTimeField(required=False, allow_null=True)
    end_date = serializers.DateTimeField(required=False, allow_null=True)
    schedule = serializers.JSONField(required=False)

    def to_internal_value(self, data):
        if isinstance(data, dict):
            forbidden = {key: FORBIDDEN_FIELD_MESSAGES.get(key, 'Это поле менять нельзя')
                         for key in data if key not in EDITABLE_FIELDS}
            if forbidden:
                raise serializers.ValidationError(forbidden)
        return super().to_internal_value(data)

    def validate_text(self, value):
        return validators.check_text(self.instance.placement, value)

    def validate_show_channel_picture(self, value):
        if value and self.instance.placement == Placement.SEARCH:
            raise serializers.ValidationError('В рекламе в поиске фото канала нет')
        return value

    def validate_start_date(self, value):
        return validators.check_date(value)

    def validate_end_date(self, value):
        return validators.check_date(value)

    def validate_schedule(self, value):
        return validators.check_schedule(self.instance.placement, value)

    def validate_media(self, value):
        return resolve_media(self.context['request'].user, value, self.instance.placement)

    def validate(self, data):
        ad = self.instance
        # Креатив после правки: новая правка собирается поверх висящей (как в Ad.update_creative)
        base = ad.pending_revision() or ad
        picture = data.get('show_channel_picture', base.show_channel_picture)
        media = data['media'] if 'media' in data else base.media
        if ('media' in data or 'show_channel_picture' in data) and media and picture:
            raise serializers.ValidationError({'media': ERR_MEDIA_WITH_PICTURE})

        # Минимальный CPM зависит от креатива (наценки за медиа и фото канала). Пока правка на проверке,
        # объявление может получить её в любой момент — поэтому берём более строгий из двух минимумов.
        # У суперадмина минимума нет, как и при создании
        if {'cpm', 'show_channel_picture', 'media'} & set(data) and not self.context['request'].user.is_superuser:
            pricing = AdPricing.load()
            min_cpm = pricing.min_cpm_for(ad.placement, ad.targeting, picture, media)
            if not ad.applies_creative_directly(self.context['request'].user):
                min_cpm = max(min_cpm, pricing.min_cpm_for(ad.placement, ad.targeting, ad.show_channel_picture,
                                                           ad.media))
            if data.get('cpm', ad.cpm) < min_cpm:
                raise serializers.ValidationError({'cpm': f"CPM can't be less than {min_cpm}"})
        return data

    def split(self):
        """(настройки — применить сразу, креатив — через ревизию)"""
        data = self.validated_data
        return ({key: value for key, value in data.items() if key in SETTINGS_FIELDS},
                {key: value for key, value in data.items() if key in AdRevision.CREATIVE_FIELDS})


class AdBudgetTopUpSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=19, decimal_places=2, min_value=Decimal('0.01'),
                                      help_text='У пользователя AdGram — целые сумы')

    def validate_amount(self, value):
        return check_budget(self.context['request'].user, value)


class BudgetOperationSerializer(serializers.ModelSerializer):
    """Операция по бюджету объявления (история из журнала MoneyOperation)"""
    exact_amount = MoneyField()
    wallet_amount = MoneyField()

    class Meta:
        model = MoneyOperation
        fields = ['id', 'kind', 'reason', 'status', 'exact_amount', 'wallet_amount', 'created_at']
        read_only_fields = fields


class AdPricingSerializer(serializers.Serializer):
    min_cpm = serializers.DictField(child=serializers.IntegerField(),
                                    help_text='Базовый минимальный CPM по месту показа, сумы')
    channel_picture_markup_percent = serializers.DecimalField(max_digits=6, decimal_places=2)
    photo_markup_percent = serializers.DecimalField(max_digits=6, decimal_places=2)
    video_markup_percent = serializers.DecimalField(max_digits=6, decimal_places=2)
    photo_specific_markup_percent = serializers.DecimalField(
        max_digits=6, decimal_places=2, help_text='Фото в сообщении в канале с выбранными конкретными каналами')
    video_specific_markup_percent = serializers.DecimalField(
        max_digits=6, decimal_places=2, help_text='Видео в сообщении в канале с выбранными конкретными каналами')
    min_budget = serializers.IntegerField()
    terms_version = serializers.CharField()

    @classmethod
    def from_pricing(cls, pricing):
        return cls({
            'min_cpm': {placement: pricing.base_cpm(placement) for placement in Placement.values},
            'channel_picture_markup_percent': pricing.channel_picture_markup_percent,
            'photo_markup_percent': pricing.photo_markup_percent,
            'video_markup_percent': pricing.video_markup_percent,
            'photo_specific_markup_percent': pricing.photo_specific_markup_percent,
            'video_specific_markup_percent': pricing.video_specific_markup_percent,
            'min_budget': pricing.min_budget,
            'terms_version': pricing.terms_version,
        })


class AdTableSettingsSerializer(serializers.Serializer):
    """Столбцы таблицы объявлений: выбранные (в порядке показа), допустимые и включённые по умолчанию"""
    columns = serializers.ListField(child=serializers.ChoiceField(choices=TABLE_COLUMNS), allow_empty=True)
    available = serializers.ListField(child=serializers.CharField(), read_only=True)
    default = serializers.ListField(child=serializers.CharField(), read_only=True)

    def validate_columns(self, value):
        return list(dict.fromkeys(value))  # без дублей, порядок сохраняем


DRAFT_MAX_BYTES = 64 * 1024


class AdDraftSerializer(serializers.Serializer):
    """Черновик формы: поля внутри не проверяем (форма бывает незаконченной) — только объект и размер"""
    data = serializers.JSONField(allow_null=True, help_text='Содержимое формы создания как есть; null — черновика нет')
    updated_at = serializers.DateTimeField(read_only=True, allow_null=True)

    def validate_data(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError('Черновик — JSON-объект')
        if len(json.dumps(value, ensure_ascii=False).encode()) > DRAFT_MAX_BYTES:
            raise serializers.ValidationError('Черновик больше 64 КБ')
        return value
