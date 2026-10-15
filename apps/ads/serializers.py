from decimal import Decimal

from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.ads import validators
from apps.ads.models import (Ad, AdPricing, AdRevision, AdStateError, InitialStatus, Placement,
                            default_schedule)
from apps.billing.models import MoneyOperation
from apps.billing.services import charge, check_budget, format_money


@extend_schema_field({'type': 'string', 'format': 'decimal', 'example': '10000.00'})
class MoneyField(serializers.Field):
    """Деньги строкой: минимум 2 знака, лишние нули справа убираем (точность не теряем)"""

    def __init__(self, **kwargs):
        kwargs['read_only'] = True
        super().__init__(**kwargs)

    def to_representation(self, value):
        return format_money(value)


class AdRevisionSerializer(serializers.ModelSerializer):
    """Правка креатива (ревизия)"""

    class Meta:
        model = AdRevision
        fields = ['id', 'text', 'show_channel_picture', 'other_info', 'status', 'reason',
                  'created_at', 'moderated_at']
        read_only_fields = fields


class AdSerializer(serializers.ModelSerializer):
    """Объявление глазами владельца (список и детали)"""
    ad_format = serializers.CharField(read_only=True, help_text='search / bot / users / channels')
    budget = MoneyField(help_text='Всего внесено в бюджет')
    spent = MoneyField()
    refunded = MoneyField()
    remaining_budget = MoneyField(help_text='Остаток: budget − spent − refunded')
    pending_revision = serializers.SerializerMethodField(
        help_text='Правка креатива на проверке (пока её не одобрят, крутится прежний вариант)')
    last_declined_revision = serializers.SerializerMethodField(
        help_text='Последняя правка, если её отклонили (с причиной); после новой правки — null')

    class Meta:
        model = Ad
        fields = [
            'id', 'placement', 'ad_format', 'title', 'text', 'url', 'show_channel_picture', 'other_info',
            'cpm', 'budget', 'spent', 'refunded', 'remaining_budget', 'daily_limit', 'views_per_user_per_day',
            'status', 'initial_status', 'start_date', 'end_date', 'schedule', 'targeting',
            'moderation_reason', 'pending_revision', 'last_declined_revision',
            'terms_version', 'terms_accepted_at', 'created_at', 'updated_at',
        ]
        read_only_fields = fields

    @extend_schema_field(AdRevisionSerializer(allow_null=True))
    def get_pending_revision(self, obj):
        revision = obj.pending_revision()
        return AdRevisionSerializer(revision).data if revision else None

    @extend_schema_field(AdRevisionSerializer(allow_null=True))
    def get_last_declined_revision(self, obj):
        revision = obj.last_declined_revision()
        return AdRevisionSerializer(revision).data if revision else None


class AdCreateSerializer(serializers.Serializer):
    """Создание объявления: проверки → списание (billing.charge) → статус IN_REVIEW"""
    placement = serializers.ChoiceField(choices=Placement.choices)
    title = serializers.CharField(max_length=255)
    text = serializers.CharField(required=False, allow_blank=True, default='',
                                 help_text='До 160 символов, без переносов строк. Для SEARCH — пусто')
    url = serializers.CharField(max_length=200, help_text='t.me/<username>')
    show_channel_picture = serializers.BooleanField(required=False, default=False)
    other_info = serializers.CharField(required=False, allow_blank=True, default='', max_length=255)
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
        errors = {}

        def check(field, func):
            try:
                return func()
            except serializers.ValidationError as exc:
                errors[field] = exc.detail

        data['text'] = check('text', lambda: validators.check_text(placement, data['text']))
        username = check('url', lambda: validators.parse_promoted_url(data['url']))
        if username:
            data['url'] = validators.promoted_url(username)
        if placement == Placement.SEARCH and data['show_channel_picture']:
            errors['show_channel_picture'] = 'В рекламе в поиске фото канала нет'
        data['schedule'] = check('schedule', lambda: validators.check_schedule(placement, data['schedule']))
        data['targeting'] = check(
            'targeting', lambda: validators.validate_targeting(placement, data['targeting'], username))

        min_cpm = pricing.min_cpm(placement, data['show_channel_picture'])
        if data['cpm'] < min_cpm:
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

    def validate(self, data):
        # Минимальный CPM зависит от фото канала (+наценка). Пока правка с фото на проверке,
        # объявление может её получить в любой момент — поэтому берём более строгий минимум
        if 'cpm' in data or 'show_channel_picture' in data:
            ad = self.instance
            pending = ad.pending_revision()
            proposed = data.get('show_channel_picture',
                                pending.show_channel_picture if pending else ad.show_channel_picture)
            if ad.applies_creative_directly(self.context['request'].user):
                picture = proposed
            else:
                picture = ad.show_channel_picture or proposed
            min_cpm = AdPricing.load().min_cpm(ad.placement, picture)
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
    min_budget = serializers.IntegerField()
    terms_version = serializers.CharField()

    @classmethod
    def from_pricing(cls, pricing):
        return cls({
            'min_cpm': {placement: pricing.base_cpm(placement) for placement in Placement.values},
            'channel_picture_markup_percent': pricing.channel_picture_markup_percent,
            'photo_markup_percent': pricing.photo_markup_percent,
            'video_markup_percent': pricing.video_markup_percent,
            'min_budget': pricing.min_budget,
            'terms_version': pricing.terms_version,
        })
