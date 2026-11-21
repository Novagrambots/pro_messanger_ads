"""
Проверки объявления v2: ссылка t.me, текст, расписание, даты и таргетинг по месту показа.

Таргетинг проверяется по справочникам (apps.directories: языки, темы, страны, локации),
а каналы и боты — через Telegram (apps.directories.telegram): канал должен быть каналом,
бот — ботом. Каналы храним объектами {username, id} — id нужен выдаче (клиент присылает
числовой channel_id в формате Bot API / TDLib: -100…), боты — по username.
"""
import re
from datetime import timedelta
from functools import lru_cache
from zoneinfo import available_timezones

from django.conf import settings
from django.utils import timezone
from rest_framework import serializers

from apps.ads.models import Placement, USERS_PLACEMENTS
from apps.directories import services as directories
from apps.directories.models import TelegramPeer
from apps.directories.telegram import (NotCheckedInTime, TelegramUnavailable, not_bot_error, not_bots,
                                      normalize_username as tg_username)

# Устройства NovaGram (пустой список = все устройства).
# Список ещё согласуем с мобильщиками — менять только здесь
DEVICES = ('ios', 'android', 'desktop')
POLITICS = ('only', 'exclude')  # показывать только в каналах о политике / не показывать в них

MAX_CHANNELS = 100  # в каждом поле со списком каналов
MAX_BOTS = 100
MAX_QUERIES = 10
# Суперадмин (свои кампании, перенос старых заказов поиска с их тегами) — без лимита Telegram в 10 фраз.
# Потолок — только чтобы один запрос не раздувался
SUPERADMIN_MAX_QUERIES = 1000
MAX_DATE_AHEAD = timedelta(days=365)
FULL_DAY_MASK = (1 << 24) - 1  # все 24 часа суток

TME_LINK_RE = re.compile(r'(?:https?://)?t\.me/([A-Za-z0-9_]+)/?', re.IGNORECASE)

# Тексты ошибок Telegram для таргетинга Каналов (как в форме ads.telegram.org)
ERR_CHANNELS_WITH_LANGUAGES = 'If specific channels are targeted, the language field must be empty'
ERR_CHANNELS_WITH_TOPICS = 'If specific channels are targeted, the topic field must be empty'
ERR_TOPICS_WITHOUT_LANGUAGE = 'You must choose one language when targeting by topic'
ERR_TELEGRAM_DOWN = 'Не удалось проверить в Telegram — он не отвечает, попробуйте ещё раз'

PeerType = TelegramPeer.PeerType


def not_checked_in_time_error(count):
    """Общая ошибка таргетинга: проверенные имена уже сохранены, со второй попытки пройдёт"""
    if count % 10 == 1 and count % 100 != 11:
        word = 'канал'
    elif 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        word = 'канала'
    else:
        word = 'каналов'
    return f'Не успели проверить в Telegram {count} {word} — нажмите «Сохранить» ещё раз'


# ===================================== Ссылка, текст =====================================

def parse_promoted_url(value):
    """Только t.me/<username> (можно с https://). Возвращает username в нижнем регистре"""
    if not TME_LINK_RE.fullmatch(value.strip()):
        raise serializers.ValidationError('Нужна ссылка вида t.me/<username>')
    try:
        return tg_username(value)
    except ValueError:
        raise serializers.ValidationError('Нужна ссылка вида t.me/<username>')


# Что можно продвигать: канал, бота или аккаунт пользователя (кнопка «написать», как «напиши админу»).
# Группы и неизвестные — нет
PROMOTABLE = (PeerType.CHANNEL, PeerType.BOT, PeerType.USER)
ERR_NOT_PROMOTABLE = '@{} — не канал, не бот и не аккаунт пользователя'


def check_promoted_peer(username, resolve_peers):
    """Продвигать можно канал, бота или аккаунт пользователя — проверяем в Telegram. Возвращает TelegramPeer"""
    try:
        peer = resolve_peers([username])[username]
    except TelegramUnavailable:
        raise serializers.ValidationError(ERR_TELEGRAM_DOWN)
    if peer.not_found:
        raise serializers.ValidationError(f'@{username} не найден в Telegram')
    if peer.peer_type not in PROMOTABLE:
        raise serializers.ValidationError(ERR_NOT_PROMOTABLE.format(username))
    return peer


def promoted_url(username):
    return f'https://t.me/{username}'


def check_text(placement, text):
    """В поиске текста нет; в остальных местах — обязателен, до 160 символов, без переносов строк"""
    text = (text or '').strip()
    if placement == Placement.SEARCH:
        if text:
            raise serializers.ValidationError('В рекламе в поиске текста нет')
        return ''
    if not text:
        raise serializers.ValidationError('Введите текст объявления')
    if len(text) > 160:
        raise serializers.ValidationError('Текст — не больше 160 символов')
    if '\n' in text or '\r' in text:
        raise serializers.ValidationError('Текст — без переносов строк')
    return text


# ===================================== Расписание, даты =====================================

@lru_cache(maxsize=1)
def _timezones():
    return available_timezones()


def check_schedule(placement, value):
    """
    {'enabled': bool, 'days': [7 масок по 24 бита, Пн…Вс], 'timezone': IANA-пояс | 'viewer'}.
    'viewer' (часовой пояс зрителя) — только для мест показа Пользователей.
    """
    if not isinstance(value, dict):
        raise serializers.ValidationError('Расписание — объект {enabled, days, timezone}')
    unknown = set(value) - {'enabled', 'days', 'timezone'}
    if unknown:
        raise serializers.ValidationError(f'Лишние поля расписания: {", ".join(sorted(unknown))}')

    enabled = value.get('enabled', False)
    days = value.get('days', [0] * 7)
    tz = value.get('timezone') or settings.TIME_ZONE

    if not isinstance(enabled, bool):
        raise serializers.ValidationError('enabled — true или false')
    if (not isinstance(days, list) or len(days) != 7
            or any(isinstance(d, bool) or not isinstance(d, int) or not 0 <= d <= FULL_DAY_MASK for d in days)):
        raise serializers.ValidationError('days — 7 чисел (Пн…Вс), каждое — маска часов от 0 до 16777215')
    if tz == 'viewer':
        if placement not in USERS_PLACEMENTS:
            raise serializers.ValidationError('Часовой пояс зрителя — только для рекламы по пользователям')
    elif not isinstance(tz, str) or tz not in _timezones():
        raise serializers.ValidationError(f'Неизвестный часовой пояс: {tz}')
    if enabled and not any(days):
        raise serializers.ValidationError('Расписание включено, но не выбран ни один час')
    return {'enabled': enabled, 'days': days, 'timezone': tz}


def check_date(value):
    """Дата старта/окончания: в будущем и не дальше года вперёд (None — убрать дату)"""
    if value is None:
        return None
    now = timezone.now()
    if value <= now:
        raise serializers.ValidationError('Дата должна быть в будущем')
    if value > now + MAX_DATE_AHEAD:
        raise serializers.ValidationError('Дата — не дальше года вперёд')
    return value


# ======================================== Таргетинг ========================================

def _as_list(value, max_items, what):
    if value is None:
        return []
    if not isinstance(value, list):
        raise serializers.ValidationError('Нужен список')
    if len(value) > max_items:
        raise serializers.ValidationError(f'Не больше {max_items} {what}')
    return value


def _unique(items, key=lambda item: item):
    """Убираем дубли, сохраняя порядок"""
    seen, result = set(), []
    for item in items:
        if key(item) not in seen:
            seen.add(key(item))
            result.append(item)
    return result


def _known(values, allowed, what):
    """Значение из справочника: неизвестное или выключенное → ошибка с самим значением"""
    unknown = [str(value) for value in values if value not in allowed]
    if unknown:
        raise serializers.ValidationError(f'Неизвестный или выключенный {what}: {", ".join(unknown)}')
    return values


def _usernames(value, max_items, what):
    names = []
    for item in _as_list(value, max_items, what):
        try:
            names.append(tg_username(item))
        except ValueError:
            raise serializers.ValidationError(f'Некорректное имя канала или бота: {item}')
    return _unique(names)


def _channels(value):
    return _usernames(value, MAX_CHANNELS, 'каналов')


def _bots(value):
    names = _usernames(value, MAX_BOTS, 'ботов')
    wrong = not_bots(names)
    if wrong:
        raise serializers.ValidationError(not_bot_error(wrong[0]))
    return names


def _queries(value, max_items=MAX_QUERIES):
    queries = []
    for item in _as_list(value, max_items, 'фраз'):
        if not isinstance(item, str) or not 4 <= len(item.strip()) <= 32:
            raise serializers.ValidationError('Каждая фраза — от 4 до 32 символов')
        queries.append(item.strip())
    queries = _unique(queries, key=str.lower)
    if not queries:
        raise serializers.ValidationError(f'Нужна хотя бы одна поисковая фраза (до {max_items})')
    return queries


def _codes(value, max_items, upper=False):
    codes = []
    for item in _as_list(value, max_items, 'значений'):
        if not isinstance(item, str) or not item.strip():
            raise serializers.ValidationError(f'Некорректный код: {item}')
        codes.append(item.strip().upper() if upper else item.strip().lower())
    return _unique(codes)


def _ids(value, max_items):
    """Список целых id (темы, локации). Строка, даже «1», — ошибка"""
    ids = []
    for item in _as_list(value, max_items, 'значений'):
        if isinstance(item, bool) or not isinstance(item, int):
            raise serializers.ValidationError(f'Нужен числовой id, а пришло: {item!r}')
        ids.append(item)
    return _unique(ids)


def _languages(value):
    return _known(_codes(value, 100), directories.language_codes(), 'язык')


def _countries(value):
    return _known(_codes(value, 300, upper=True), directories.country_codes(), 'код страны')


def _topics(value):
    return _known(_ids(value, 100), directories.topic_ids(), 'id темы')


def _locations(value):
    return _known(_ids(value, 100), directories.location_countries(), 'id локации')


def _devices(value):
    devices = _unique([item.lower() if isinstance(item, str) else item
                       for item in _as_list(value, len(DEVICES), 'устройств')])
    for device in devices:
        if device not in DEVICES:
            raise serializers.ValidationError(f'Устройство — одно из: {", ".join(DEVICES)}')
    return devices


def _politics(value):
    if value is not None and value not in POLITICS:
        raise serializers.ValidationError('politics — null, "only" или "exclude"')
    return value


_USERS_FIELDS = {
    'countries': _countries,
    'locations': _locations,
    'languages': _languages,
    'topics': _topics,
    'channel_audiences': _channels,
    'exclude_topics': _topics,
    'exclude_channel_audiences': _channels,
    'devices': _devices,
}

# Какие поля таргетинга есть у каждого места показа
TARGETING_FIELDS = {
    Placement.SEARCH: {'queries': _queries},
    Placement.BOT: {'bots': _bots},
    Placement.CHANNEL_MESSAGE: {
        'languages': _languages,
        'topics': _topics,
        'channels': _channels,
        'exclude_topics': _topics,
        'exclude_channels': _channels,
    },
    Placement.USERS_CHANNEL_MESSAGE: {**_USERS_FIELDS, 'politics': _politics},
    Placement.USERS_VIDEO_BANNER: {**_USERS_FIELDS, 'politics': _politics},
    Placement.USERS_BOT_BANNER: _USERS_FIELDS,  # в баннере в боте галочек про политику нет
}
# Поля, где каждое имя должно оказаться каналом (храним {username, id}) или ботом (по username)
CHANNEL_FIELDS = ('channels', 'exclude_channels', 'channel_audiences', 'exclude_channel_audiences')
BOT_FIELDS = ('bots',)


def validate_targeting(placement, data, promoted_peer, resolve_peers, superadmin=False):
    """
    Строгая проверка таргетинга по месту показа: формат и лимиты → справочники → каналы
    и боты через Telegram (resolve_peers(имена) → {имя: TelegramPeer}) → правила места показа.
    Возвращает нормализованный словарь (все поля места показа на месте). Ошибки — по полям.
    superadmin — фраз поиска можно больше 10 (до SUPERADMIN_MAX_QUERIES); остальные правила те же
    """
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise serializers.ValidationError('Таргетинг — объект')

    fields = dict(TARGETING_FIELDS[placement])
    if superadmin and 'queries' in fields:
        fields['queries'] = lambda value: _queries(value, SUPERADMIN_MAX_QUERIES)
    unknown = set(data) - set(fields)
    if unknown:
        raise serializers.ValidationError(
            f'Для места показа {placement} нет полей: {", ".join(sorted(unknown))}')

    result, errors = {}, {}
    for key, check in fields.items():
        try:
            result[key] = check(data.get(key))
        except serializers.ValidationError as exc:
            errors[key] = exc.detail
    if errors:
        raise serializers.ValidationError(errors)

    _check_peers(result, resolve_peers)

    if placement == Placement.CHANNEL_MESSAGE:
        _channel_message_rules(result, promoted_peer)
    elif placement in USERS_PLACEMENTS:
        _users_rules(result)
    return result


def _check_peers(targeting, resolve_peers):
    """Каждое имя в полях каналов — канал, в полях ботов — бот. Каналы → {username, id}"""
    names = [name for key in CHANNEL_FIELDS + BOT_FIELDS for name in targeting.get(key, [])]
    if not names:
        return
    try:
        peers = resolve_peers(names)
    except NotCheckedInTime as exc:
        raise serializers.ValidationError(not_checked_in_time_error(exc.count))
    except TelegramUnavailable:
        raise serializers.ValidationError(ERR_TELEGRAM_DOWN)

    errors = {}
    for key in CHANNEL_FIELDS + BOT_FIELDS:
        if key not in targeting:
            continue
        expected, what = (PeerType.BOT, 'бот') if key in BOT_FIELDS else (PeerType.CHANNEL, 'канал')
        problems = []
        for name in targeting[key]:
            peer = peers[name]
            if peer.not_found:
                problems.append(f'@{name} не найден в Telegram')
            elif peer.peer_type != expected:
                problems.append(f'@{name} — не {what}')
        if problems:
            errors[key] = '; '.join(problems)
        elif key in CHANNEL_FIELDS:
            targeting[key] = [{'username': name, 'id': peers[name].telegram_id} for name in targeting[key]]
    if errors:
        raise serializers.ValidationError(errors)


def _channel_message_rules(targeting, promoted_peer):
    """Правила Telegram: либо языки + темы, либо явный список каналов"""
    errors = {}
    if targeting['channels']:
        if targeting['languages']:
            errors['languages'] = ERR_CHANNELS_WITH_LANGUAGES
        if targeting['topics']:
            errors['topics'] = ERR_CHANNELS_WITH_TOPICS
    elif targeting['topics'] and not targeting['languages']:
        errors['languages'] = ERR_TOPICS_WITHOUT_LANGUAGE
    elif not targeting['languages']:
        errors['languages'] = 'Укажите языки каналов или конкретные каналы — иначе реклама нигде не покажется'
    if errors:
        raise serializers.ValidationError(errors)

    # Реклама канала не показывается в нём самом (выдача сверяет по id)
    if promoted_peer and promoted_peer.peer_type == PeerType.CHANNEL:
        excluded = targeting['exclude_channels']
        if not any(item['username'] == promoted_peer.username for item in excluded):
            excluded.append({'username': promoted_peer.username, 'id': promoted_peer.telegram_id})


def _users_rules(targeting):
    if not targeting['countries']:
        raise serializers.ValidationError({'countries': 'Выберите хотя бы одну страну'})
    # Аккаунт пользователя на «только Android» — можно: Android с 1.4.0 его рисует, а старым версиям
    # выдача такие объявления не отдаёт (selection.skips_user_accounts)
    # Локация должна быть в одной из выбранных стран
    location_countries = directories.location_countries()
    wrong = [str(location) for location in targeting['locations']
             if location_countries[location] not in targeting['countries']]
    if wrong:
        raise serializers.ValidationError({'locations': f'Локации не из выбранных стран: {", ".join(wrong)}'})
