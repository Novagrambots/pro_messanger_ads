from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.ads.serializers import MoneyField
from apps.directories.models import Language, Topic

from .models import ChannelEarning, EarningTransaction

# Суммы заработка — 6 знаков в базе; наружу через format_money: ровные как раньше («12.35»),
# дробные — полностью, без хвостовых нулей


class PublicEarningSerializer(serializers.ModelSerializer):
    """Публичная витрина-«крючок»: сколько канал уже заработал. Без суммы к выводу."""
    is_claimed = serializers.SerializerMethodField()
    total_earned = MoneyField()

    class Meta:
        model = ChannelEarning
        fields = ['channel_id', 'channel_name', 'total_earned', 'total_impressions', 'is_claimed']

    @extend_schema_field(serializers.BooleanField())
    def get_is_claimed(self, obj):
        return obj.claim_status == ChannelEarning.ClaimStatus.CONFIRMED


class MyEarningSerializer(serializers.ModelSerializer):
    """Заработок для подтверждённого владельца (+ язык и темы канала)"""
    share_rate = serializers.SerializerMethodField()
    available = MoneyField()
    total_earned = MoneyField()
    language = serializers.CharField(source='language_id', allow_null=True, read_only=True)
    meta_locked = serializers.SerializerMethodField(help_text='Язык и темы проверил модератор — владелец их не меняет')

    class Meta:
        model = ChannelEarning
        fields = [
            'channel_id', 'channel_name', 'available', 'total_earned', 'total_impressions',
            'claim_status', 'share_rate', 'claimed_at', 'language', 'topics', 'meta_locked',
        ]

    def get_meta_locked(self, obj) -> bool:
        return obj.meta_updated_role == ChannelEarning.MetaRole.MODERATOR

    @extend_schema_field(serializers.DecimalField(max_digits=4, decimal_places=3))
    def get_share_rate(self, obj):
        return obj.get_share_rate()


class AdminEarningSerializer(serializers.ModelSerializer):
    """Заработок канала глазами суперадмина (админ-панель)"""
    owner = serializers.SerializerMethodField()
    share_rate = serializers.SerializerMethodField()
    available = MoneyField()
    total_earned = MoneyField()

    class Meta:
        model = ChannelEarning
        fields = [
            'channel_id', 'channel_name', 'owner', 'claim_status', 'claimed_at',
            'available', 'total_earned', 'total_impressions', 'share_rate', 'created_at',
        ]

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_owner(self, obj):
        # Владельца может не быть (канал ещё не заявлен) или его удалили
        return obj.owner.username if obj.owner else None

    @extend_schema_field(serializers.DecimalField(max_digits=4, decimal_places=3))
    def get_share_rate(self, obj):
        return obj.get_share_rate()


class AdminEarningsSummarySerializer(serializers.Serializer):
    """Итоги по всем каналам списка заработков (суммы — как в списке, строкой)"""
    total_earned = MoneyField()
    available = MoneyField()
    channels = serializers.IntegerField(read_only=True, help_text='Сколько каналов в списке')


class ClaimRequestSerializer(serializers.Serializer):
    channel_id = serializers.IntegerField(required=True)
    # Необязательная метка-имя для витрины (если клиент его знает).
    channel_name = serializers.CharField(max_length=255, required=False, allow_blank=True)


class WithdrawRequestSerializer(serializers.Serializer):
    channel_id = serializers.IntegerField(required=True)


class EarningTransactionSerializer(serializers.ModelSerializer):
    amount = MoneyField()

    class Meta:
        model = EarningTransaction
        fields = ['kind', 'amount', 'impressions', 'order', 'ad', 'created_at', 'updated_at']


class ChannelMetaSerializer(serializers.Serializer):
    """Язык (код из справочника) и 1–3 темы канала. PATCH: можно прислать что-то одно"""
    language = serializers.CharField(required=False, help_text='Код языка из /api/ads/directories/languages/')
    topics = serializers.ListField(child=serializers.IntegerField(min_value=1), required=False, min_length=1,
                                   max_length=3, help_text='1–3 id тем из /api/ads/directories/topics/')

    def validate_language(self, value):
        language = Language.objects.filter(code=value, is_active=True).first()
        if language is None:
            raise serializers.ValidationError(f'Неизвестный или выключенный язык: {value}')
        return language

    def validate_topics(self, value):
        topics = list(dict.fromkeys(value))  # без дублей, порядок сохраняем
        known = set(Topic.objects.filter(id__in=topics, is_active=True).values_list('id', flat=True))
        unknown = [str(topic) for topic in topics if topic not in known]
        if unknown:
            raise serializers.ValidationError(f'Неизвестная или выключенная тема: {", ".join(unknown)}')
        return topics

    def validate(self, data):
        if not data:
            raise serializers.ValidationError('Укажите language и/или topics')
        return data


class ModerationChannelSerializer(serializers.ModelSerializer):
    """Канал партнёрки глазами модератора: язык, темы и кто их менял"""
    owner = serializers.CharField(source='owner.username', default=None, read_only=True)
    language = serializers.CharField(source='language_id', allow_null=True, read_only=True)
    meta_updated_by = serializers.CharField(source='meta_updated_by.username', default=None, read_only=True)

    class Meta:
        model = ChannelEarning
        fields = ['channel_id', 'channel_name', 'owner', 'claim_status', 'language', 'topics',
                  'meta_updated_by', 'meta_updated_role', 'meta_updated_at']
        read_only_fields = fields


class MessageSerializer(serializers.Serializer):
    message = serializers.CharField()
