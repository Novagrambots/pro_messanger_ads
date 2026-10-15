"""
Проверки объявления v2, которые не зависят от базы: ссылка t.me, текст, расписание, даты
и таргетинг по месту показа.

Каналы и боты пока храним t.me-именами (в id их превратит этап 5). Справочники языков,
тем и стран тоже подключит этап 5 — сейчас проверяем только формат значений.
"""
import re
from datetime import timedelta
from functools import lru_cache
from zoneinfo import available_timezones

from django.conf import settings
from django.utils import timezone
from rest_framework import serializers

from apps.ads.models import Placement, USERS_PLACEMENTS

# Устройства NovaGram (пустой список = все устройства).
# Список ещё согласуем с мобильщиками — менять только здесь
DEVICES = ('ios', 'android', 'desktop')
POLITICS = ('only', 'exclude')  # показывать только в каналах о политике / не показывать в них

MAX_CHANNELS = 100  # в каждом поле со списком каналов
MAX_BOTS = 100
MAX_QUERIES = 10
MAX_DATE_AHEAD = timedelta(days=365)
FULL_DAY_MASK = (1 << 24) - 1  # все 24 часа суток

# Имя в Telegram: латиница, цифры, _, начинается с буквы, 4–32 символа (короткие бывают у купленных имён)
USERNAME_RE = re.compile(r'[a-z][a-z0-9_]{3,31}')
TME_LINK_RE = re.compile(r'(?:https?://)?t\.me/([A-Za-z0-9_]+)/?', re.IGNORECASE)
LANGUAGE_RE = re.compile(r'[a-z]{2,3}')
COUNTRY_RE = re.compile(r'[A-Z]{2}')

# Тексты ошибок Telegram для таргетинга Каналов (как в форме ads.telegram.org)
ERR_CHANNELS_WITH_LANGUAGES = 'If specific channels are targeted, the language field must be empty'
ERR_CHANNELS_WITH_TOPICS = 'If specific channels are targeted, the topic field must be empty'
ERR_TOPICS_WITHOUT_LANGUAGE = 'You must choose one language when targeting by topic'


# ===================================== Ссылка, текст =====================================

def parse_promoted_url(value):
    """Только t.me/<username> (можно с https://). Возвращает username в нижнем регистре"""
    match = TME_LINK_RE.fullmatch(value.strip())
    username = match.group(1).lower() if match else ''
    if not USERNAME_RE.fullmatch(username):
        raise serializers.ValidationError('Нужна ссылка вида t.me/<username>')
    return username


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


def normalize_username(value):
    """t.me/name, https://t.me/name, @name или name → name в нижнем регистре"""
    if not isinstance(value, str):
        raise serializers.ValidationError(f'Некорректное имя: {value}')
    raw = value.strip()
    match = TME_LINK_RE.fullmatch(raw)
    name = (match.group(1) if match else raw.removeprefix('@')).lower()
    if not USERNAME_RE.fullmatch(name):
        raise serializers.ValidationError(f'Некорректное имя канала или бота: {value}')
    return name


def _channels(value):
    names = [normalize_username(item) for item in _as_list(value, MAX_CHANNELS, 'каналов')]
    return _unique(names)


def _bots(value):
    names = _unique([normalize_username(item) for item in _as_list(value, MAX_BOTS, 'ботов')])
    for name in names:
        if not name.endswith('bot'):
            raise serializers.ValidationError(f'@{name} — не бот: имя бота кончается на «bot»')
    return names


def _queries(value):
    queries = []
    for item in _as_list(value, MAX_QUERIES, 'фраз'):
        if not isinstance(item, str) or not 4 <= len(item.strip()) <= 32:
            raise serializers.ValidationError('Каждая фраза — от 4 до 32 символов')
        queries.append(item.strip())
    queries = _unique(queries, key=str.lower)
    if not queries:
        raise serializers.ValidationError('Нужна хотя бы одна поисковая фраза (до 10)')
    return queries


def _strings(value, regex=None, max_len=64, max_items=100, upper=False, lower=False):
    result = []
    for item in _as_list(value, max_items, 'значений'):
        if not isinstance(item, str) or not item.strip() or len(item.strip()) > max_len:
            raise serializers.ValidationError(f'Некорректное значение: {item}')
        item = item.strip()
        item = item.upper() if upper else item.lower() if lower else item
        if regex and not regex.fullmatch(item):
            raise serializers.ValidationError(f'Некорректный код: {item}')
        result.append(item)
    return _unique(result, key=str.lower)


def _languages(value):
    return _strings(value, regex=LANGUAGE_RE, lower=True)


def _countries(value):
    return _strings(value, regex=COUNTRY_RE, upper=True, max_items=250)


def _topics(value):
    return _strings(value)


def _locations(value):
    return _strings(value, max_len=100)


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


def validate_targeting(placement, data, promoted_username):
    """
    Строгая проверка таргетинга по месту показа. Возвращает нормализованный словарь
    (все поля места показа на месте). Ошибки — по полям.
    """
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise serializers.ValidationError('Таргетинг — объект')

    fields = TARGETING_FIELDS[placement]
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

    if placement == Placement.CHANNEL_MESSAGE:
        _channel_message_rules(result, promoted_username)
    elif placement in USERS_PLACEMENTS and not result['countries']:
        raise serializers.ValidationError({'countries': 'Выберите хотя бы одну страну'})
    return result


def _channel_message_rules(targeting, promoted_username):
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

    # Реклама канала не показывается в нём самом
    if promoted_username and promoted_username not in targeting['exclude_channels']:
        targeting['exclude_channels'].append(promoted_username)
