from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.ads.models import AdDecision, AdRevision
from apps.ads.serializers import AdMediaBriefSerializer, AdRevisionSerializer, AdSerializer


class AdOwnerSerializer(serializers.Serializer):
    """Владелец объявления глазами модератора"""
    user_id = serializers.UUIDField()
    username = serializers.CharField()
    adgram_username = serializers.CharField(help_text='Имя в AdGram (пусто, если вошёл не через AdGram)')


class ModerationAdSerializer(AdSerializer):
    """Объявление глазами модератора: как у владельца + кто владелец"""
    owner = AdOwnerSerializer(source='user', read_only=True)

    class Meta(AdSerializer.Meta):
        fields = AdSerializer.Meta.fields + ['owner']
        read_only_fields = fields


class AdDecisionSerializer(serializers.ModelSerializer):
    moderator = serializers.CharField(source='moderator.username', default=None, read_only=True)
    revision_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = AdDecision
        fields = ['action', 'reason', 'moderator', 'revision_id', 'created_at']
        read_only_fields = fields


class ModerationAdDetailSerializer(ModerationAdSerializer):
    """Полная карточка для модератора: + все правки креатива и история решений"""
    revisions = AdRevisionSerializer(many=True, read_only=True)
    decisions = AdDecisionSerializer(many=True, read_only=True)

    class Meta(ModerationAdSerializer.Meta):
        fields = ModerationAdSerializer.Meta.fields + ['revisions', 'decisions']
        read_only_fields = fields


class CreativeSerializer(serializers.Serializer):
    text = serializers.CharField()
    show_channel_picture = serializers.BooleanField()
    other_info = serializers.CharField()
    media = AdMediaBriefSerializer(allow_null=True)


class ModerationRevisionSerializer(serializers.ModelSerializer):
    """Правка на проверке: текущая версия объявления и новая — рядом"""
    ad_id = serializers.UUIDField(read_only=True)
    ad_title = serializers.CharField(source='ad.title', read_only=True)
    placement = serializers.CharField(source='ad.placement', read_only=True)
    ad_status = serializers.CharField(source='ad.status', read_only=True)
    url = serializers.CharField(source='ad.url', read_only=True)
    owner = AdOwnerSerializer(source='ad.user', read_only=True)
    current = serializers.SerializerMethodField()
    proposed = serializers.SerializerMethodField()

    class Meta:
        model = AdRevision
        fields = ['id', 'ad_id', 'ad_title', 'placement', 'ad_status', 'url', 'owner',
                  'current', 'proposed', 'status', 'created_at']
        read_only_fields = fields

    def _creative(self, obj):
        creative = {field: getattr(obj, field) for field in AdRevision.CREATIVE_FIELDS}
        if creative['media'] is not None:
            creative['media'] = AdMediaBriefSerializer(creative['media'], context=self.context).data
        return creative

    @extend_schema_field(CreativeSerializer)
    def get_current(self, obj):
        return self._creative(obj.ad)

    @extend_schema_field(CreativeSerializer)
    def get_proposed(self, obj):
        return self._creative(obj)
