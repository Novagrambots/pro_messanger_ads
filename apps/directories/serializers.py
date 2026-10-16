from rest_framework import serializers

from apps.directories.models import Country, Language, Location, TelegramPeer, Topic

NAME_FIELDS = ['name_en', 'name_ru', 'name_uz']


class LanguageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Language
        fields = ['code', *NAME_FIELDS]


class TopicSerializer(serializers.ModelSerializer):
    class Meta:
        model = Topic
        fields = ['id', *NAME_FIELDS]


class CountrySerializer(serializers.ModelSerializer):
    class Meta:
        model = Country
        fields = ['code', *NAME_FIELDS]


class LocationSerializer(serializers.ModelSerializer):
    country = serializers.CharField(source='country_id', help_text='Код страны')

    class Meta:
        model = Location
        fields = ['id', 'country', *NAME_FIELDS, 'region_en', 'region_ru', 'region_uz']


class TelegramPeerSerializer(serializers.ModelSerializer):
    """Данные канала / бота по t.me-ссылке (для чипа, превью и проверки таргетинга)"""
    avatar_url = serializers.SerializerMethodField()

    class Meta:
        model = TelegramPeer
        fields = ['username', 'peer_type', 'telegram_id', 'title', 'avatar_url', 'subscribers', 'monthly_users']
        read_only_fields = fields

    def get_avatar_url(self, obj) -> str | None:
        if not obj.avatar:
            return None
        request = self.context.get('request')
        return request.build_absolute_uri(obj.avatar.url) if request else obj.avatar.url
