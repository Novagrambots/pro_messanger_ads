"""
Счётчики выдачи для мониторинга трафика (DEV-77): сколько запросов /api/ads/serve/ пришло и сколько из них
получили объявление (fill rate) — по клиенту: платформа · версия приложения · продукт из User-Agent
(Novagram/89, okhttp/3.14.9). Так видно, что трафик конкретной сборки упал (как у iOS 89 в ноябре).

Горячий путь: один поход в Redis выдачи (HINCRBY в хеш часа), без SQL; ошибка Redis выдачу не ломает.
Часы — по Ташкенту. Хеш живёт 3 суток, в Postgres его переносит задача monitor_serve_traffic
(apps/ads/monitoring.py)
"""
import logging
import re
import time
from datetime import timedelta

import redis
from django.utils import timezone

from . import audience, selection
from . import store as S

logger = logging.getLogger(__name__)

HOUR_TTL = int(timedelta(days=3).total_seconds())
NONE = '-'  # части клиента нет (платформу не прислали, User-Agent без продукта…)
SEP = '|'   # разделитель в поле хеша; в нормализованных частях его не бывает
REQUESTS, FILLED = 'n', 'f'
# Первый продукт User-Agent: «Novagram/89 CFNetwork/… Darwin/…» → Novagram/89, «okhttp/3.14.9» → okhttp/3.14.9
_UA_RE = re.compile(r'\s*([A-Za-z][A-Za-z0-9._-]{0,31})/([A-Za-z0-9._-]{1,24})')
_last_error_log = 0.0


def hour_key(hour):
    """Хеш часа 'YYYYMMDDHH' (Ташкент): поля «n|клиент» — запросы, «f|клиент» — с объявлением"""
    return f'{S.PREFIX}mon:{hour}'


def hour_of(moment=None):
    """Час по Ташкенту: '2026112213'"""
    return (moment or timezone.now()).astimezone(S.tz()).strftime('%Y%m%d%H')


def client_of(platform, app_version, user_agent):
    """
    Клиент — (платформа, версия, продукт UA): короткие нормализованные строки, '-' если части нет.
    Нормализуем, чтобы мусор в полях не плодил тысячи разных клиентов
    """
    device = audience.normalize_device(platform) or NONE
    version = selection.parse_app_version(app_version)
    version = '.'.join(str(part) for part in version[:4])[:24] if version else NONE
    match = _UA_RE.match(user_agent) if isinstance(user_agent, str) else None
    product = f'{match.group(1)}/{match.group(2)}' if match else NONE
    return device, version, product


def field(kind, client):
    return SEP.join((kind, *client))


def parse_field(name):
    """«n|ios|1.2|Novagram/89» → ('n', ('ios', '1.2', 'Novagram/89')); чужое поле → (None, None)"""
    parts = name.split(SEP)
    if len(parts) != 4 or parts[0] not in (REQUESTS, FILLED):
        return None, None
    return parts[0], tuple(parts[1:])


def count_serve(client, filled):
    """+1 запрос клиента в хеш текущего часа (и +1 «с объявлением», если оно нашлось)"""
    global _last_error_log
    key = hour_key(hour_of())
    try:
        pipe = S.store().redis.pipeline(transaction=False)
        pipe.hincrby(key, field(REQUESTS, client), 1)
        if filled:
            pipe.hincrby(key, field(FILLED, client), 1)
        pipe.expire(key, HOUR_TTL)
        pipe.execute()
    except redis.RedisError:
        # Мониторинг не важнее выдачи: ответ уже готов, в лог — не чаще раза в минуту на процесс
        if time.monotonic() - _last_error_log > 60:
            _last_error_log = time.monotonic()
            logger.warning('Счётчик мониторинга выдачи не записан: Redis выдачи недоступен')


def read_hour(hour):
    """Хеш часа → {клиент: [запросы, с объявлением]}"""
    result = {}
    for name, value in S.store().redis.hgetall(hour_key(hour)).items():
        kind, client = parse_field(name)
        if kind is None:
            continue
        counts = result.setdefault(client, [0, 0])
        counts[0 if kind == REQUESTS else 1] += int(value)
    return result
