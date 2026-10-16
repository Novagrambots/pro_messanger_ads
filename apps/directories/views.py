from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from apps.common.schema import ERROR_RESPONSE
from apps.directories import services
from apps.directories.serializers import (CountrySerializer, LanguageSerializer, LocationSerializer,
                                          SimilarBotSerializer, SimilarChannelSerializer, TelegramPeerSerializer,
                                          TopicSerializer)
from apps.directories.similar import SimilarUnavailable, find_similar, parse_usernames
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


class SimilarThrottle(UserRateThrottle):
    scope = 'tg_similar'  # 20 в минуту на пользователя (settings.DEFAULT_THROTTLE_RATES)


class BaseSimilarView(APIView):
    """
    Похожие (через AdGram → ads.telegram.org). Ответ кэшируется на 7 дней по набору имён.
    503 — похожие сейчас недоступны: фронт прячет кнопку «Add similar …».
    Создание объявлений от этого не зависит — каналы и боты проверяет резолвер t.me.
    """
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [SimilarThrottle]
    kind = param = serializer_class = None

    def get(self, request):
        try:
            names = parse_usernames(request.query_params.get(self.param, ''), self.kind)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        try:
            peers = find_similar(self.kind, names)
        except SimilarUnavailable:
            return Response({'error': 'Похожие сейчас недоступны'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(self.serializer_class(peers, many=True, context={'request': request}).data)


SIMILAR_ERRORS = {400: ERROR_RESPONSE, 503: ERROR_RESPONSE}


class SimilarChannelsView(BaseSimilarView):
    """Похожие каналы для 1–10 каналов: ?channels=kunuz,daryo"""
    kind, param, serializer_class = 'channels', 'channels', SimilarChannelSerializer

    @extend_schema(parameters=[OpenApiParameter('channels', str, required=True,
                                                 description='1–10 имён каналов через запятую')],
                   responses={200: SimilarChannelSerializer(many=True), **SIMILAR_ERRORS})
    def get(self, request):
        return super().get(request)


class SimilarBotsView(BaseSimilarView):
    """Похожие боты для 1–9 ботов: ?bots=a_bot,b_bot"""
    kind, param, serializer_class = 'bots', 'bots', SimilarBotSerializer

    @extend_schema(parameters=[OpenApiParameter('bots', str, required=True,
                                                 description='1–9 имён ботов через запятую')],
                   responses={200: SimilarBotSerializer(many=True), **SIMILAR_ERRORS})
    def get(self, request):
        return super().get(request)

