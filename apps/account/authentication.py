import re

import jwt
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError
from drf_spectacular.contrib.rest_framework_simplejwt import SimpleJWTScheme
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework import authentication, exceptions

from apps.account.services import create_user_with_balance


User = get_user_model()

# Юзеры AdGram получают username = "ag_" + userId. Регистрация такие имена не пускает
ADGRAM_USERNAME_PREFIX = 'ag_'
# userId у AdGram — cuid (латиница и цифры). Берём с запасом, но без мусора в username
ADGRAM_USER_ID_RE = re.compile(r'[A-Za-z0-9_-]{1,64}')


def check_adgram_secret():
    """
    Секрет AdGram не должен совпадать с ключом, которым simplejwt подписывает наши токены.
    Иначе наши токены модераторов будут проходить как токены AdGram (без userId) и получать 401.
    Вызывается при старте приложения (AccountConfig.ready).
    """
    secret = settings.ADGRAM_JWT_SECRET
    simplejwt_key = settings.SIMPLE_JWT.get('SIGNING_KEY', settings.SECRET_KEY)
    if secret and secret == simplejwt_key:
        raise ImproperlyConfigured(
            'ADGRAM_JWT_SECRET совпадает с SECRET_KEY. Задайте в .env отдельный секрет AdGram '
            '(тот, которым AdGram подписывает свои JWT), иначе сломается вход модераторов и суперадмина.'
        )


class AdGramJWTAuthentication(authentication.BaseAuthentication):
    """
    Вход по токену AdGram: `Authorization: Bearer <token>`, JWT HS256 с payload
    {userId, username, role}. Подпись проверяем общим секретом, в AdGram не ходим.

    Если токен подписан не секретом AdGram — возвращаем None, дальше его проверит
    simplejwt (свой логин new_dvs). Роль из токена НЕ читаем: ADMIN в AdGram
    не даёт в new_dvs никаких прав — все юзеры AdGram здесь рекламодатели.
    """
    keyword = 'Bearer'

    def authenticate(self, request):
        secret = settings.ADGRAM_JWT_SECRET
        if not secret:
            return None  # вход через AdGram выключен

        token = self._get_token(request)
        if token is None:
            return None

        payload = self._decode(token, secret)
        if payload is None:
            return None  # токен не от AdGram — пусть проверяет simplejwt

        user = self._get_or_create_user(payload.get('userId'))
        return user, payload

    def authenticate_header(self, request):
        # Без этого DRF отвечал бы 403 вместо 401 на запросы без токена (смотрит на первый класс)
        return f'{self.keyword} realm="api"'

    def _get_token(self, request):
        """Достаёт токен из заголовка `Authorization: Bearer <token>`"""
        parts = authentication.get_authorization_header(request).split()
        if len(parts) != 2 or parts[0].lower() != self.keyword.lower().encode():
            return None
        try:
            return parts[1].decode()
        except UnicodeError:
            return None

    def _decode(self, token, secret):
        """Проверяет подпись и срок. None — токен не наш (чужая подпись или вообще не JWT)"""
        try:
            return jwt.decode(token, secret, algorithms=['HS256'], options={'require': ['exp']})
        except (jwt.InvalidSignatureError, jwt.InvalidAlgorithmError, jwt.DecodeError):
            return None
        except jwt.ExpiredSignatureError:
            raise exceptions.AuthenticationFailed('Срок токена AdGram истёк')
        except jwt.InvalidTokenError:
            # Подпись наша, но токен кривой (нет exp, дата выдачи в будущем и т.п.)
            raise exceptions.AuthenticationFailed('Некорректный токен AdGram')

    def _get_or_create_user(self, adgram_user_id):
        if not isinstance(adgram_user_id, str) or not ADGRAM_USER_ID_RE.fullmatch(adgram_user_id):
            raise exceptions.AuthenticationFailed('В токене AdGram нет корректного userId')

        user = User.objects.filter(adgram_user_id=adgram_user_id).first()
        if user is None:
            user = self._create_user(adgram_user_id)

        if not user.is_active:
            raise exceptions.AuthenticationFailed('Пользователь отключён')
        return user

    def _create_user(self, adgram_user_id):
        """Первый вход юзера AdGram: создаём рекламодателя с балансом (как при регистрации)"""
        try:
            return create_user_with_balance(
                username=ADGRAM_USERNAME_PREFIX + adgram_user_id,
                adgram_user_id=adgram_user_id,
            )
        except IntegrityError:
            # Два первых запроса пришли одновременно: второй упал на unique — берём того, кого создал первый
            user = User.objects.filter(adgram_user_id=adgram_user_id).first()
            if user is None:
                # unique сработал не на adgram_user_id, а на username — имя уже кем-то занято
                raise exceptions.AuthenticationFailed('Не удалось создать пользователя AdGram: имя уже занято')
            return user


class BearerJWTScheme(SimpleJWTScheme):
    """Swagger: одна Bearer-схема на оба вида токенов (заменяет стандартную схему simplejwt)"""
    priority = 1

    def get_security_definition(self, auto_schema):
        definition = super().get_security_definition(auto_schema)
        definition['description'] = (
            'Один из двух токенов: access-токен new_dvs (из /api/auth/login/ — модераторы и суперадмин) '
            'или JWT AdGram (рекламодатели входят через AdGram).'
        )
        return definition


class AdGramJWTScheme(OpenApiAuthenticationExtension):
    """
    Swagger: токен AdGram идёт в том же заголовке Bearer, поэтому отдельной схемы нет —
    его описывает BearerJWTScheme выше (иначе spectacular ругается на две схемы с одним именем).
    """
    target_class = 'apps.account.authentication.AdGramJWTAuthentication'
    name = []

    def get_security_requirement(self, auto_schema):
        return None

    def get_security_definition(self, auto_schema):
        return []
