"""
Гео зрителя по IP: база DB-IP Lite City (mmdb, CC BY 4.0 — «IP Geolocation by DB-IP»).

Файл лежит вне git (settings.GEOIP_PATH), beat раз в месяц скачивает свежий и подменяет его
атомарно (os.replace); каждый процесс замечает новый файл и переоткрывает базу. Файла нет —
гео по IP нет (страна только по phone_country), без ошибок.
"""
import gzip
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import timedelta

import maxminddb
import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

RECHECK_EVERY = 60  # как часто проверять, не сменился ли файл, с
DBIP_URL = 'https://download.db-ip.com/free/dbip-city-lite-{month}.mmdb.gz'
DOWNLOAD_TIMEOUT = (10, 120)
MAX_DOWNLOAD_SIZE = 1024 ** 3  # распакованный файл больше 1 ГБ — что-то не то


def client_ip(request):
    """
    IP клиента. settings.CLIENT_IP_HEADER — заголовок, которому доверяем (за Cloudflare —
    CF-Connecting-IP). Пусто → первый адрес X-Forwarded-For, иначе REMOTE_ADDR
    """
    if settings.CLIENT_IP_HEADER:
        value = request.headers.get(settings.CLIENT_IP_HEADER, '')
    else:
        value = request.headers.get('X-Forwarded-For', '').split(',')[0]
    return value.strip() or request.META.get('REMOTE_ADDR', '')


@dataclass(frozen=True)
class GeoInfo:
    country: str      # ISO-2 или None
    cities: tuple     # названия города (на всех языках базы)
    regions: tuple    # названия регионов (области)


def _names(item):
    return tuple(name for name in ((item or {}).get('names') or {}).values() if name)


class _Database:
    """Открытая база; переоткрывается, если файл подменили. Потокобезопасно"""

    def __init__(self):
        self.lock = threading.Lock()
        self.reader = None
        self.path = None
        self.stamp = None
        self.checked_at = 0.0
        self.missing_logged = False

    def get(self):
        now = time.monotonic()
        if self.path == settings.GEOIP_PATH and now - self.checked_at < RECHECK_EVERY:
            return self.reader
        with self.lock:
            self.checked_at = now
            path = settings.GEOIP_PATH
            try:
                stat = os.stat(path)
            except OSError:
                if not self.missing_logged:
                    logger.warning('Гео-базы нет (%s) — страна только по phone_country, города нет', path)
                    self.missing_logged = True
                self.reader, self.path, self.stamp = None, path, None
                return None
            stamp = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
            if path != self.path or stamp != self.stamp:
                try:
                    reader = maxminddb.open_database(path)
                except (OSError, ValueError, maxminddb.InvalidDatabaseError):
                    logger.exception('Гео-база %s не открылась — работаем без неё', path)
                    reader = None
                self.reader, self.path, self.stamp = reader, path, stamp
                self.missing_logged = False
            return self.reader


_database = _Database()


def lookup(ip):
    """IP → GeoInfo или None (нет базы, кривой IP, адреса нет в базе)"""
    reader = _database.get()
    if reader is None or not ip:
        return None
    try:
        record = reader.get(ip)
    except ValueError:
        return None
    if not record:
        return None
    country = ((record.get('country') or {}).get('iso_code') or '').upper() or None
    regions = tuple(name for item in record.get('subdivisions') or [] for name in _names(item))
    return GeoInfo(country, _names(record.get('city')), regions)


# ===================================== Обновление базы =====================================

def _months(now):
    """Текущий месяц и прошлый: DB-IP выкладывает файл в начале месяца"""
    first = now.replace(day=1)
    previous = (first - timedelta(days=1)).replace(day=1)
    return [first.strftime('%Y-%m'), previous.strftime('%Y-%m')]


def update_database(now=None):
    """
    Скачать свежую DB-IP Lite City и атомарно подменить файл. Возвращает месяц базы.
    Не скачалось или файл битый — старая база остаётся, ошибка наружу (beat повторит через месяц,
    руками — manage.py update_geoip).
    """
    path = settings.GEOIP_PATH
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = f'{path}.download'
    last_error = None
    for month in _months(now or timezone.now()):
        try:
            with requests.get(DBIP_URL.format(month=month), stream=True, timeout=DOWNLOAD_TIMEOUT) as response:
                if response.status_code == 404:
                    last_error = f'{month}: файла ещё нет'
                    continue
                response.raise_for_status()
                response.raw.decode_content = True
                with gzip.GzipFile(fileobj=response.raw) as source, open(tmp, 'wb') as target:
                    _copy_limited(source, target)
            _check_database(tmp)
            os.replace(tmp, path)
            logger.info('Гео-база обновлена: DB-IP Lite City %s', month)
            return month
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    raise RuntimeError(f'Гео-база не скачана: {last_error}')


def _copy_limited(source, target):
    copied = 0
    while chunk := source.read(1024 * 1024):
        copied += len(chunk)
        if copied > MAX_DOWNLOAD_SIZE:
            raise RuntimeError('Гео-база больше 1 ГБ — не похоже на DB-IP Lite City')
        target.write(chunk)


def _check_database(path):
    """Открывается и это база городов — иначе не подменяем"""
    with maxminddb.open_database(path) as reader:
        if 'city' not in reader.metadata().database_type.lower():
            raise RuntimeError(f'Не та база: {reader.metadata().database_type}')
