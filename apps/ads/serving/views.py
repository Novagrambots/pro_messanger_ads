"""
Горячий путь выдачи — простые Django-вьюхи с JSON (без DRF). serve и events не ходят в Postgres;
жалобы редкие — их пишем в Postgres сразу.
Доступ — X-API-Key (сравнение за постоянное время) + лимиты запросов в Redis.
"""
import json
import logging
import re
import time

import redis
from django.conf import settings
from django.db import DatabaseError
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.ads.models import Ad, AdReport
from apps.common.permissions import has_server_api_key
from apps.directories.telegram import normalize_username

from . import events as ad_events
from . import audience, geo, selection, snapshot
from . import store as S
from . import tokens
from .selection import Slot

logger = logging.getLogger(__name__)

MAX_QUERY_LENGTH = 256
CHANNEL_ID_RE = re.compile(r'-100\d{10,13}')  # id канала как в Bot API / TDLib
COUNTRY_RE = re.compile(r'[A-Za-z]{2}')        # phone_country — ISO-2 из кода номера телефона
MAX_REPORT_DETAILS = 500
CLIENT_FIELDS = ('lang', 'platform', 'app_version', 'phone_country')
_last_outage_log = 0.0


def _error(message, status):
    return JsonResponse({'error': message}, status=status)


def _log_outage(message):
    """Redis / БД недоступны: в лог не чаще раза в 10 с на процесс (иначе тысячи строк в секунду)"""
    global _last_outage_log
    if time.monotonic() - _last_outage_log > 10:
        _last_outage_log = time.monotonic()
        logger.exception(message)


def _parse(request):
    """Общие проверки → (data, viewer, None) или (None, None, ответ с ошибкой)"""
    if not has_server_api_key(request):
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


def _bot_username(value):
    """
    Имя бота из контекста: @, t.me-ссылка и регистр не важны. Некорректное имя → None.
    Суффикс «bot» не требуем: у @BotFather его нет, а тип бота проверили при создании объявления
    """
    try:
        return normalize_username(value)
    except ValueError:
        return None


def _channel_id(value):
    """id канала из контекста: число или строка в формате -100… Иначе None"""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    value = str(value).strip()
    return int(value) if CHANNEL_ID_RE.fullmatch(value) else None


def viewer_profile(snap, data, request, history):
    """
    Профиль для формата «Пользователи» — только в памяти: страна по phone_country (иначе по IP),
    локации — город по IP и его область (если IP из той же страны), язык, устройство,
    история каналов и темы интересов (темы каналов из истории)
    """
    world = snap.world
    country = (data.get('phone_country') or '').upper()
    country = country if country in world.countries else None
    place = geo.lookup(geo.client_ip(request))
    if country is None and place is not None and place.country in world.countries:
        country = place.country
    locations = frozenset()
    if place is not None and country is not None and place.country == country:
        locations = world.locations.resolve(country, place.cities, place.regions)
    topics = frozenset().union(*(snap.channels[channel].topics for channel in history if channel in snap.channels))
    return audience.Viewer(
        country=country, locations=locations, language=audience.normalize_language(data.get('lang'), world.languages),
        device=audience.normalize_device(data.get('platform')), channels=history, topics=topics,
    )


def _public_ad(entry, request, random_id, video=None):
    """
    Объявление для клиента: публичная часть снимка + random_id, ссылки на файлы — абсолютные.
    В видео (как у Telegram) — только текст: без медиа и кнопки, зато со временем показа
    """
    ad = {'random_id': random_id, **entry.payload}
    promoted = ad['promoted']
    if promoted['avatar_url']:
        ad['promoted'] = {**promoted, 'avatar_url': request.build_absolute_uri(promoted['avatar_url'])}
    if video is not None:
        del ad['media'], ad['button_type']
        ad['min_display_duration'] = video['min_display_duration']
        ad['max_display_duration'] = video['max_display_duration']
    elif ad['media']:
        ad['media'] = {**ad['media'], 'url': request.build_absolute_uri(ad['media']['url'])}
    return ad


@csrf_exempt
@require_POST
def serve(request):
    """POST /api/ads/serve/ — объявление для показа. Ничего не списывает"""
    data, viewer, error = _parse(request)
    if error:
        return error

    slot = data.get('placement')  # место в приложении: SEARCH, BOT, CHANNEL_MESSAGE, VIDEO
    if slot not in Slot.ALL:
        return _error(f'placement — одно из: {", ".join(Slot.ALL)}', 400)
    context = data.get('context') or {}
    if not isinstance(context, dict):
        return _error('context — объект', 400)
    for field in CLIENT_FIELDS:
        value = data.get(field)
        if value is not None and (not isinstance(value, str) or len(value) > 64):
            return _error(f'{field} — строка до 64 символов', 400)
    if data.get('phone_country') is not None and not COUNTRY_RE.fullmatch(data['phone_country']):
        return _error('phone_country — код страны ISO-2 (по коду номера телефона), например UZ', 400)

    key = ''  # контекст слота: фраза поиска, имя бота или id канала
    if slot == Slot.SEARCH:
        query = context.get('query')
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_LENGTH:
            return _error(f'context.query — поисковый запрос до {MAX_QUERY_LENGTH} символов', 400)
        key = snapshot.normalize_query(query)
    elif slot == Slot.BOT:
        key = _bot_username(context.get('bot_username'))
        if key is None:
            return _error('context.bot_username — имя бота в Telegram', 400)
    else:
        key = _channel_id(context.get('channel_id'))
        if key is None:
            return _error('context.channel_id — id канала в формате -100… (как в Bot API)', 400)
        msg_id = context.get('msg_id')
        if slot == Slot.VIDEO and (isinstance(msg_id, bool) or not isinstance(msg_id, int) or msg_id <= 0):
            return _error('context.msg_id — id сообщения с видео (целое > 0)', 400)

    try:
        # Лимиты 7а и история каналов зрителя — одним походом в Redis
        limited, history = S.rate_limit_and_history(
            [('serve', viewer, settings.ADS_RATE_VIEWER_SERVE), ('ip', geo.client_ip(request), settings.ADS_RATE_IP)],
            viewer, add_channel=key if slot in Slot.IN_CHANNEL else None, read_history=slot != Slot.SEARCH)
        if limited:
            return _error('Слишком много запросов', 429)
        snap = snapshot.current()
        profile = viewer_profile(snap, data, request, history) if slot != Slot.SEARCH else None
        skip_users = selection.skips_user_accounts(data.get('platform'), data.get('app_version'))
        entry = selection.pick(snap, slot, key, viewer, profile=profile, skip_user_accounts=skip_users)
    except (redis.RedisError, DatabaseError):
        # Без Redis выдачи нет: пустой ответ с коротким кэшем на клиенте
        _log_outage('Выдача объявлений недоступна')
        return JsonResponse({'ads': [], 'ttl_seconds': settings.ADS_EMPTY_TTL})

    video = snap.world.video if slot == Slot.VIDEO else None
    ads = []
    if entry is not None:
        # Канал (сообщение, видео) — в токене: по нему начислится заработок партнёру
        channel_id = key if slot in Slot.IN_CHANNEL else None
        random_id = tokens.issue(entry.id, viewer, entry.placement, tokens.context_hash(slot, key),
                                 channel_id=channel_id)
        ads.append(_public_ad(entry, request, random_id, video))
    response = {'ads': ads, 'ttl_seconds': settings.ADS_CACHE_TTL}
    if video is not None:
        response.update(start_delay=video['start_delay'], between_delay=video['between_delay'])
    return JsonResponse(response)


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
                              ('ip', geo.client_ip(request), settings.ADS_RATE_IP)]):
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
                              ('ip', geo.client_ip(request), settings.ADS_RATE_IP)]):
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
