import jwt
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from drf_spectacular.contrib.rest_framework_simplejwt import SimpleJWTScheme
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework import authentication, exceptions

from apps.account.services import ADGRAM_USER_ID_RE, AdGramUsernameTaken, get_or_create_adgram_user


User = get_user_model()

# Короткий токен AdGram для NovaGram (GET /api/novagram/token, 15 минут) — только с этим aud.
# Основной 7-дневный токен AdGram (без aud) сюда не пускаем
ADGRAM_AUDIENCE = 'novagram'
# Роль в AdGram, которая в new_dvs даёт суперадмина
ADGRAM_ADMIN_ROLE = 'ADMIN'


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
    {userId, username, role, aud: "novagram", exp}. Подпись проверяем общим секретом, в AdGram не ходим.

    Если токен подписан не секретом AdGram — возвращаем None, дальше его проверит
    simplejwt (свой логин new_dvs). Подпись наша, но aud нет или он другой — 401.

    Роль: ADMIN в AdGram = суперадмин в new_dvs. is_superuser у ag_… сверяется с токеном
    на каждом запросе (источник правды — AdGram, ручная правка перезапишется).
    Поле role (ADVERTISER / MODERATOR) токен не трогает — модераторов хранит new_dvs.
    is_staff не ставим и пароля нет — в Django-админку юзеры AdGram не входят.
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

        user = self._get_or_create_user(payload.get('userId'), self._clean_username(payload.get('username')),
                                        payload.get('role') == ADGRAM_ADMIN_ROLE)
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
        """Проверяет подпись, срок и aud. None — токен не наш (чужая подпись или вообще не JWT)"""
        try:
            return jwt.decode(token, secret, algorithms=['HS256'], audience=ADGRAM_AUDIENCE,
                              options={'require': ['exp', 'aud']})
        except (jwt.InvalidSignatureError, jwt.InvalidAlgorithmError, jwt.DecodeError):
            return None
        except jwt.ExpiredSignatureError:
            raise exceptions.AuthenticationFailed('Срок токена AdGram истёк')
        except jwt.InvalidTokenError:
            # Подпись наша, но токен кривой (нет exp или aud, чужой aud, дата выдачи в будущем и т.п.)
            raise exceptions.AuthenticationFailed('Некорректный токен AdGram')

    @staticmethod
    def _clean_username(name):
        """Имя из токена — только для отображения. Не строка — не трогаем (None)"""
        if not isinstance(name, str):
            return None
        return name.strip()[:255]

    def _get_or_create_user(self, adgram_user_id, adgram_username, is_admin):
        if not isinstance(adgram_user_id, str) or not ADGRAM_USER_ID_RE.fullmatch(adgram_user_id):
            raise exceptions.AuthenticationFailed('В токене AdGram нет корректного userId')

        try:
            user = get_or_create_adgram_user(adgram_user_id, adgram_username or '')
        except AdGramUsernameTaken:
            raise exceptions.AuthenticationFailed('Не удалось создать пользователя AdGram: имя уже занято')

        # Имя и админство из AdGram — обновляем одним UPDATE и только когда что-то реально изменилось
        changes = {}
        if adgram_username is not None and adgram_username != user.adgram_username:
            changes['adgram_username'] = adgram_username
        if is_admin != user.is_superuser:
            changes['is_superuser'] = is_admin
        if changes:
            User.objects.filter(pk=user.pk).update(**changes)
            for field, value in changes.items():
                setattr(user, field, value)

        if not user.is_active:
            raise exceptions.AuthenticationFailed('Пользователь отключён')
        return user


class BearerJWTScheme(SimpleJWTScheme):
    """Swagger: одна Bearer-схема на оба вида токенов (заменяет стандартную схему simplejwt)"""
    priority = 1

    def get_security_definition(self, auto_schema):
        definition = super().get_security_definition(auto_schema)
        definition['description'] = (
            'Один из двух токенов: короткий JWT AdGram (GET /api/novagram/token на сайте AdGram, 15 минут, '
            'aud "novagram") — основной вход для всех: рекламодатели, модераторы, админы AdGram (= суперадмин); '
            'или access-токен new_dvs из /api/auth/login/ — аварийный вход суперадмина Django и старые модераторы.'
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
