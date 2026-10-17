"""
Горячий путь выдачи — простые Django-вьюхи с JSON (без DRF). serve и events не ходят в Postgres;
жалобы редкие — их пишем в Postgres сразу.
Доступ — X-API-Key (сравнение за постоянное время) + лимиты запросов в Redis.
"""
import json
import logging
import time

import redis
from django.conf import settings
from django.db import DatabaseError
from django.http import JsonResponse
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.ads.models import Ad, AdReport, Placement

from . import events as ad_events
from . import selection, snapshot
from . import store as S
from . import tokens

logger = logging.getLogger(__name__)

MAX_QUERY_LENGTH = 256
MAX_REPORT_DETAILS = 500
CLIENT_FIELDS = ('lang', 'platform', 'app_version')  # принимаем, для поиска пока не используются
_last_outage_log = 0.0


def _error(message, status):
    return JsonResponse({'error': message}, status=status)


def _log_outage(message):
    """Redis / БД недоступны: в лог не чаще раза в 10 с на процесс (иначе тысячи строк в секунду)"""
    global _last_outage_log
    if time.monotonic() - _last_outage_log > 10:
        _last_outage_log = time.monotonic()
        logger.exception(message)


def _has_api_key(request):
    """Как HasServerApiKey: ключ не задан на бэкенде — доступ закрыт"""
    expected = settings.AD_SERVER_API_KEY
    provided = request.headers.get('X-API-Key')
    return bool(expected and provided and constant_time_compare(provided, expected))


def _client_ip(request):
    # Настоящий IP ставит Cloudflare; без него — то, что передал nginx
    return (request.headers.get('CF-Connecting-IP') or request.headers.get('X-Real-IP')
            or request.META.get('REMOTE_ADDR', ''))


def _parse(request):
    """Общие проверки → (data, viewer, None) или (None, None, ответ с ошибкой)"""
    if not _has_api_key(request):
        return None, None, _error('Неверный или отсутствующий API-ключ.', 403)
    if not tokens.is_configured():
        logger.error('Выдача объявлений не настроена: нет VIEWER_HASH_SECRET или ADS_TOKEN_SECRET')
        return None, None, _error('Выдача рекламы временно недоступна', 503)
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        data = None
    if not isinstance(data, dict):
        return None, None, _error('Тело запроса — JSON-объект', 400)
    user_id = tokens.parse_user_id(data.get('user_id'))
    if user_id is None:
        return None, None, _error('user_id — числовой id пользователя Telegram', 400)
    return data, tokens.viewer_key(user_id), None


def _public_ad(entry, request, random_id):
    """Объявление для клиента: публичная часть снимка + random_id, ссылки на файлы — абсолютные"""
    ad = {'random_id': random_id, **entry.payload}
    promoted = ad['promoted']
    if promoted['avatar_url']:
        ad['promoted'] = {**promoted, 'avatar_url': request.build_absolute_uri(promoted['avatar_url'])}
    if ad['media']:
        ad['media'] = {**ad['media'], 'url': request.build_absolute_uri(ad['media']['url'])}
    return ad


@csrf_exempt
@require_POST
def serve(request):
    """POST /api/ads/serve/ — объявление для показа. Ничего не списывает"""
    data, viewer, error = _parse(request)
    if error:
        return error

    placement = data.get('placement')
    if placement not in Placement.values:
        return _error(f'placement — одно из: {", ".join(Placement.values)}', 400)
    context = data.get('context') or {}
    if not isinstance(context, dict):
        return _error('context — объект', 400)
    for field in CLIENT_FIELDS:
        value = data.get(field)
        if value is not None and (not isinstance(value, str) or len(value) > 64):
            return _error(f'{field} — строка до 64 символов', 400)

    phrase = ''
    if placement == Placement.SEARCH:
        query = context.get('query')
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_LENGTH:
            return _error(f'context.query — поисковый запрос до {MAX_QUERY_LENGTH} символов', 400)
        phrase = snapshot.normalize_query(query)

    try:
        if S.over_rate_limit([('serve', viewer, settings.ADS_RATE_VIEWER_SERVE),
                              ('ip', _client_ip(request), settings.ADS_RATE_IP)]):
            return _error('Слишком много запросов', 429)
        entry = selection.pick(snapshot.current(), placement, phrase, viewer)
    except (redis.RedisError, DatabaseError):
        # Без Redis выдачи нет: пустой ответ с коротким кэшем на клиенте
        _log_outage('Выдача объявлений недоступна')
        return JsonResponse({'ads': [], 'ttl_seconds': settings.ADS_EMPTY_TTL})

    ads = []
    if entry is not None:
        random_id = tokens.issue(entry.id, viewer, placement, tokens.context_hash(placement, phrase))
        ads.append(_public_ad(entry, request, random_id))
    return JsonResponse({'ads': ads, 'ttl_seconds': settings.ADS_CACHE_TTL})


@csrf_exempt
@require_POST
def events(request):
    """POST /api/ads/events/ — показы и клики (до 50 в пачке). Каждое событие — отдельно"""
    data, viewer, error = _parse(request)
    if error:
        return error

    items = data.get('events')
    if not isinstance(items, list) or not 1 <= len(items) <= ad_events.MAX_EVENTS:
        return _error(f'events — список от 1 до {ad_events.MAX_EVENTS} событий', 400)

    try:
        if S.over_rate_limit([('events', viewer, settings.ADS_RATE_VIEWER_EVENTS),
                              ('ip', _client_ip(request), settings.ADS_RATE_IP)]):
            return _error('Слишком много запросов', 429)
        accepted, rejected = ad_events.process(snapshot.current(), viewer, items)
    except (redis.RedisError, DatabaseError):
        # Клиент копит события и пришлёт позже — деньги не теряются
        _log_outage('Приём событий объявлений недоступен')
        return _error('Приём событий временно недоступен, пришлите позже', 503)
    return JsonResponse({'accepted': accepted, 'rejected': rejected})


@csrf_exempt
@require_POST
def report(request):
    """POST /api/ads/report/ — жалоба зрителя на показанное объявление (одна от зрителя на объявление)"""
    data, viewer, error = _parse(request)
    if error:
        return error

    reason = data.get('reason')
    if reason not in AdReport.Reason.values:
        return _error(f'reason — одно из: {", ".join(AdReport.Reason.values)}', 400)
    details = data.get('details') or ''
    if not isinstance(details, str) or len(details) > MAX_REPORT_DETAILS:
        return _error(f'details — строка до {MAX_REPORT_DETAILS} символов', 400)
    token = tokens.decode(data.get('random_id'))
    if token is None or token.viewer != viewer:
        return _error('random_id недействителен или просрочен', 400)

    try:
        if S.over_rate_limit([('report', viewer, settings.ADS_RATE_VIEWER_REPORTS),
                              ('ip', _client_ip(request), settings.ADS_RATE_IP)]):
            return _error('Слишком много запросов', 429)
    except redis.RedisError:
        # Без Redis лимит не проверить — жалобу всё равно принимаем (их мало, одна от зрителя на объявление)
        _log_outage('Лимит жалоб не проверен: Redis выдачи недоступен')

    try:
        if not Ad.objects.filter(pk=token.ad_id).exists():
            return _error('Объявление не найдено', 404)
        _, created = AdReport.objects.get_or_create(
            ad_id=token.ad_id, viewer_key=viewer, defaults={'reason': reason, 'details': details.strip()})
    except DatabaseError:
        _log_outage('Жалоба не сохранена: база недоступна')
        return _error('Жалобы временно не принимаются, попробуйте позже', 503)
    # Повторная жалоба того же зрителя — не ошибка: первая уже у модератора
    return JsonResponse({'reported': True}, status=201 if created else 200)
