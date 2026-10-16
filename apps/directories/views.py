from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from apps.common.schema import ERROR_RESPONSE
from apps.directories import services
from apps.directories.serializers import (CountrySerializer, LanguageSerializer, LocationSerializer,
                                          TelegramPeerSerializer, TopicSerializer)
from apps.directories.telegram import TelegramUnavailable, normalize_username, resolve


class LanguagesView(APIView):
    """Языки (71, как у Telegram) — только активные. Кэш на час"""
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: LanguageSerializer(many=True)})
    def get(self, request):
        return Response(services.languages())


class TopicsView(APIView):
    """Темы каналов (41, id как у Telegram) — только активные. Кэш на час"""
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: TopicSerializer(many=True)})
    def get(self, request):
        return Response(services.topics())


class CountriesView(APIView):
    """Страны (ISO 3166-1 + XK) — только активные. Кэш на час"""
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: CountrySerializer(many=True)})
    def get(self, request):
        return Response(services.countries())


class LocationsView(APIView):
    """Локации: ?country=UZ и поиск ?q= по названию на любом языке (до 50 штук). Кэш на час"""
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(parameters=[
        OpenApiParameter('country', str, description='Код страны, например UZ'),
        OpenApiParameter('q', str, description='Часть названия (en / ru / uz)'),
    ], responses={200: LocationSerializer(many=True)})
    def get(self, request):
        return Response(services.locations(request.query_params.get('country', ''),
                                           request.query_params.get('q', '')))


class ResolveThrottle(UserRateThrottle):
    scope = 'tg_resolve'  # 60 в минуту на пользователя (settings.DEFAULT_THROTTLE_RATES)


class TelegramResolveView(APIView):
    """
    Данные канала / бота по t.me-ссылке: тип, числовой id, название, аватар, подписчики.
    Фронт резолвит каналы по одному, по мере ввода (чипы). Кэш: свежие данные — сутки, «не найден» — час.
    """
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ResolveThrottle]

    @extend_schema(parameters=[OpenApiParameter('url', str, required=True,
                                                 description='t.me/<username>, @username или username')],
                   responses={200: TelegramPeerSerializer, 400: ERROR_RESPONSE, 404: ERROR_RESPONSE,
                              503: ERROR_RESPONSE})
    def get(self, request):
        try:
            username = normalize_username(request.query_params.get('url', ''))
        except ValueError:
            return Response({'error': 'Нужна ссылка вида t.me/<username>'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            peer = resolve(username)
        except TelegramUnavailable:
            return Response({'error': 'Telegram сейчас не отвечает, попробуйте ещё раз'},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)
        if peer.not_found:
            return Response({'error': f'@{username} не найден в Telegram'}, status=status.HTTP_404_NOT_FOUND)
        return Response(TelegramPeerSerializer(peer, context={'request': request}).data)
