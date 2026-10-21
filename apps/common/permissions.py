from django.conf import settings
from django.utils.crypto import constant_time_compare
from rest_framework import permissions


def server_api_keys():
    """
    Действующие серверные ключи: AD_SERVER_API_KEYS (через запятую) + старый AD_SERVER_API_KEY.
    Несколько ключей — чтобы при смене ключа старые версии приложения работали, пока их не обновят
    """
    keys = settings.AD_SERVER_API_KEYS.split(',') + [settings.AD_SERVER_API_KEY or '']
    return [key.strip() for key in keys if key.strip()]


def has_server_api_key(request):
    """
    Заголовок X-API-Key совпал с одним из ключей. Сверяем со ВСЕМИ ключами через
    constant_time_compare, без выхода на первом совпадении — время ответа не подсказывает ключ.
    Ключей на бэкенде нет — доступ закрыт (fail-closed)
    """
    provided = request.headers.get('X-API-Key')
    if not provided:
        return False
    matched = False
    for key in server_api_keys():
        matched |= constant_time_compare(provided, key)
    return matched


class HasServerApiKey(permissions.BasePermission):
    """
    Пускает только запросы с валидным серверным API-ключом (server-to-server):
    заголовок X-API-Key — один из ключей AD_SERVER_API_KEYS / AD_SERVER_API_KEY (см. has_server_api_key).
    Если ключей на бэкенде нет — доступ закрыт: пустой .env не должен открывать
    эндпоинты показа/клика рекламы.
    """
    message = 'Неверный или отсутствующий API-ключ.'

    def has_permission(self, request, view):
        return has_server_api_key(request)


class IsModerator(permissions.BasePermission):
    """Пускает только модераторов и суперадмина (проверка модерации рекламы)."""
    message = 'Требуются права модератора.'

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_moderator)


class IsSuperAdmin(permissions.BasePermission):
    """Пускает только суперадмина: ADMIN из AdGram или createsuperuser (аварийный вход)."""
    message = 'Требуются права суперадмина.'

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_superuser)
