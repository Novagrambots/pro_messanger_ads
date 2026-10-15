from decimal import Decimal

from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.ads import validators
from apps.ads.models import Ad, AdPricing, AdStateError, InitialStatus, Placement, default_schedule
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


class AdSerializer(serializers.ModelSerializer):
    """Объявление глазами владельца (список и детали)"""
    ad_format = serializers.CharField(read_only=True, help_text='search / bot / users / channels')
    budget = MoneyField(help_text='Всего внесено в бюджет')
    spent = MoneyField()
    refunded = MoneyField()
    remaining_budget = MoneyField(help_text='Остаток: budget − spent − refunded')

    class Meta:
        model = Ad
        fields = [
            'id', 'placement', 'ad_format', 'title', 'text', 'url', 'show_channel_picture', 'other_info',
            'cpm', 'budget', 'spent', 'refunded', 'remaining_budget', 'daily_limit', 'views_per_user_per_day',
            'status', 'initial_status', 'start_date', 'end_date', 'schedule', 'targeting',
            'moderation_reason', 'terms_version', 'terms_accepted_at', 'created_at', 'updated_at',
        ]
        read_only_fields = fields


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
            return Ad.objects.create(
                user=user, budget=budget, terms_version=pricing.terms_version,
                terms_accepted_at=timezone.now(), **validated_data,
            )

        return charge(user, paid, MoneyOperation.OrderType.ADS, create_ad)


# Что владелец может менять без модерации — остальное отдаёт 400 с причиной
EDITABLE_FIELDS = {'cpm', 'daily_limit', 'views_per_user_per_day', 'status', 'start_date', 'end_date', 'schedule'}
_CREATIVE_MESSAGE = 'Креатив меняется через повторную модерацию (будет на этапе 4)'
FORBIDDEN_FIELD_MESSAGES = {
    'targeting': 'Таргетинг после создания не меняется',
    'placement': 'Место показа после создания не меняется',
    'title': _CREATIVE_MESSAGE,
    'text': _CREATIVE_MESSAGE,
    'url': _CREATIVE_MESSAGE,
    'show_channel_picture': _CREATIVE_MESSAGE,
    'other_info': _CREATIVE_MESSAGE,
    'budget': 'Бюджет пополняется через POST /api/ads/<id>/budget/',
}


class AdUpdateSerializer(serializers.Serializer):
    """PATCH владельца: только то, что меняется без модерации"""
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

    def validate_cpm(self, value):
        min_cpm = AdPricing.load().min_cpm(self.instance.placement, self.instance.show_channel_picture)
        if value < min_cpm:
            raise serializers.ValidationError(f"CPM can't be less than {min_cpm}")
        return value

    def validate_start_date(self, value):
        return validators.check_date(value)

    def validate_end_date(self, value):
        return validators.check_date(value)

    def validate_schedule(self, value):
        return validators.check_schedule(self.instance.placement, value)


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
