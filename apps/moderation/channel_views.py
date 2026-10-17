"""
Каналы партнёрки глазами модератора: язык и темы (для рекламы в каналах по языкам и темам).
Модератор может поправить любой канал — после этого владелец их не меняет.
"""
from django.db import transaction
from django.db.models import Q
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.pagination import StandardResultsSetPagination
from apps.common.permissions import IsModerator
from apps.common.schema import ERROR_RESPONSE
from apps.partner.models import ChannelEarning
from apps.partner.serializers import ChannelMetaSerializer, ModerationChannelSerializer
from apps.partner.views import update_channel_meta


class ModerationChannelsListView(generics.ListAPIView):
    """Каналы партнёрки (и заявленные, и нет). ?missing_meta=1 — без языка или без тем; ?claim_status="""
    serializer_class = ModerationChannelSerializer
    permission_classes = [IsModerator]
    pagination_class = StandardResultsSetPagination

    @extend_schema(parameters=[
        OpenApiParameter('missing_meta', str, enum=['1'], description='1 — только каналы без языка или без тем'),
        OpenApiParameter('claim_status', str, enum=ChannelEarning.ClaimStatus.values),
    ])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        queryset = ChannelEarning.objects.select_related('owner', 'meta_updated_by').order_by('-total_earned', 'pk')
        params = self.request.query_params
        if params.get('missing_meta') == '1':
            queryset = queryset.filter(Q(language__isnull=True) | Q(topics=[]))
        if params.get('claim_status') in ChannelEarning.ClaimStatus.values:
            queryset = queryset.filter(claim_status=params['claim_status'])
        return queryset


class ModerationChannelMetaView(APIView):
    """Поправить язык и темы любого канала (свой канал модератор не правит — суперадмин может)"""
    permission_classes = [IsModerator]

    @extend_schema(request=ChannelMetaSerializer, responses={200: ModerationChannelSerializer,
                                                             403: ERROR_RESPONSE, 404: ERROR_RESPONSE})
    def patch(self, request, channel_id):
        serializer = ChannelMetaSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            earning = ChannelEarning.objects.select_for_update().filter(channel_id=int(channel_id)).first()
            if earning is None:
                return Response({'error': 'Канал не найден'}, status=status.HTTP_404_NOT_FOUND)
            if earning.owner_id == request.user.id and not request.user.is_superuser:
                return Response({'error': 'Нельзя модерировать собственный канал'}, status=status.HTTP_403_FORBIDDEN)
            update_channel_meta(earning, serializer.validated_data, request.user, ChannelEarning.MetaRole.MODERATOR)
        return Response(ModerationChannelSerializer(earning).data)
