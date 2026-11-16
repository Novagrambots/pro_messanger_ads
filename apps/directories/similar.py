"""
Похожие каналы и боты — через внутреннее API AdGram (оно ходит на ads.telegram.org).

- ads_ref — непрозрачный хэш ads.telegram.org: только для запросов похожих, это НЕ telegram_id
  и в таргетинг не попадает. Запоминаем навсегда (обновляем, если AdGram вернул другой).
- Данные из AdGram не считаются проверенными резолвером: новые записи TelegramPeer создаём
  «протухшими» — при добавлении в таргетинг резолвер сходит в Telegram за числовым id.
  Существующим записям обновляем только ads_ref, название, счётчики и пустой аватар.
- Аватары качаем в конце разом, параллельно и не дольше AVATAR_SECONDS: не успели — отдаём без
  аватара, новые записи всё равно проверит резолвер в фоне (задача refresh_peers) и докачает.
- Время этапов (search / similar / аватары) пишем в лог одной строкой.
- Ответ кэшируем на 7 дней по набору входных имён (в кэше — только имена, сам ответ
  собирается из TelegramPeer, чтобы аватары и названия были свежими).
- AdGram недоступен / 429 / 503 → SimilarUnavailable (фронт спрячет кнопку). После 429 —
  «остываем» по Retry-After (не дольше 10 минут), чтобы не долбить AdGram и Telegram.
"""
import hashlib
import logging
import time
from datetime import timedelta

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from apps.billing import adgram_client
from apps.billing.adgram_client import AdGramNotConfigured, AdGramRejected, AdGramUnavailable
from apps.directories.models import TelegramPeer
from apps.directories.tasks import refresh_peers
from apps.directories.telegram import (Deadline, DeadlineExceeded, download_cdn_avatar, normalize_username,
                                       not_bot_error, not_bots, run_parallel, store_avatar)

logger = logging.getLogger(__name__)

CACHE_TTL = 7 * 24 * 60 * 60
COOLDOWN_KEY = 'similar:cooldown'
MAX_COOLDOWN = 10 * 60
REQUEST_SECONDS = 20  # общий лимит времени на запрос
AVATAR_SECONDS = 3  # на все аватары разом
ADGRAM_READ_TIMEOUT = 10

# Канал или бот: функции AdGram (по имени — чтобы их можно было подменить в тестах),
# тип записи, лимит входных имён и какой счётчик приходит
KINDS = {
    'channels': {'search': 'search_channel', 'similar': 'similar_channels', 'max_inputs': 10,
                 'peer_type': TelegramPeer.PeerType.CHANNEL, 'count_field': 'subscribers'},
    'bots': {'search': 'search_bot', 'similar': 'similar_bots', 'max_inputs': 9,
             'peer_type': TelegramPeer.PeerType.BOT, 'count_field': 'monthly_users'},
}


class SimilarUnavailable(Exception):
    """Похожие сейчас недоступны (AdGram не настроен, лежит, лимит Telegram)"""


def find_similar(kind, usernames):
    """Похожие для имён (уже нормализованных). Список TelegramPeer в порядке AdGram"""
    key = f'similar:{kind}:' + hashlib.md5(','.join(sorted(usernames)).encode()).hexdigest()
    names = cache.get(key)
    if names is None:
        names = _fetch(kind, usernames)
        cache.set(key, names, CACHE_TTL)
    peers = {peer.username: peer for peer in TelegramPeer.objects.filter(username__in=names)}
    return [peers[name] for name in names if name in peers]


class _Run:
    """Один поход в AdGram: новые записи, аватары на скачивание и время этапов (для лога)"""

    def __init__(self, kind, usernames):
        self.config, self.kind, self.inputs = KINDS[kind], kind, len(usernames)
        self.created = []  # имена новых записей TelegramPeer — их проверит резолвер в фоне
        self.avatars = []  # (TelegramPeer, photo_url) — качаем в конце разом
        self.searches, self.avatars_saved = 0, 0
        self.times = {}
        self._lap_started = time.monotonic()

    def lap(self, stage):
        now = time.monotonic()
        self.times[stage] = now - self._lap_started
        self._lap_started = now

    def log(self, result, level=logging.INFO):
        def took(stage):
            return f'{self.times[stage]:.1f} с' if stage in self.times else '—'

        logger.log(level, 'Похожие %s: имён %s, search %s (запросов %s), similar %s, аватары %s/%s за %s → %s',
                   self.kind, self.inputs, took('search'), self.searches, took('similar'),
                   self.avatars_saved, len(self.avatars), took('avatars'), result)


def _call(run, name, *args, deadline):
    timeout = deadline.timeout(ADGRAM_READ_TIMEOUT)
    return getattr(adgram_client, run.config[name])(*args, timeout=(min(2, timeout), timeout))


def _fetch(kind, usernames):
    if not adgram_client.is_configured() or cache.get(COOLDOWN_KEY):
        raise SimilarUnavailable()

    run = _Run(kind, usernames)
    deadline = Deadline(REQUEST_SECONDS)
    try:
        refs = [ref for ref in (_ads_ref(run, name, deadline) for name in usernames) if ref]
        run.lap('search')
        results = _call(run, 'similar', refs, deadline=deadline) if refs else []
        run.lap('similar')
    except AdGramUnavailable as exc:
        if exc.retry_after:
            cache.set(COOLDOWN_KEY, True, min(exc.retry_after, MAX_COOLDOWN))
        run.log(f'503 (AdGram не ответил: {exc})', logging.WARNING)
        raise SimilarUnavailable() from None
    except (AdGramRejected, AdGramNotConfigured) as exc:
        logger.error('AdGram: запрос похожих отклонён: %s', exc)  # 401 / 400 — ошибка настройки или наш баг
        raise SimilarUnavailable() from None
    except DeadlineExceeded:
        run.log(f'503 (кончились {REQUEST_SECONDS} с)', logging.WARNING)
        raise SimilarUnavailable() from None

    names, inputs = [], set(usernames)
    for item in results:
        username = _username(item.username)
        if username and username not in inputs and username not in names:
            _save(run, item, username)
            names.append(username)

    _save_avatars(run, Deadline(min(AVATAR_SECONDS, deadline.left())))
    run.lap('avatars')
    run.log(f'найдено {len(names)}')
    if run.created:
        transaction.on_commit(lambda: _check_in_background(run.created))
    return names


def _ads_ref(run, username, deadline):
    """ads_ref имени: из базы, иначе поиском в AdGram (и запоминаем). None — нет на ads.telegram.org"""
    ref = TelegramPeer.objects.filter(username=username).values_list('ads_ref', flat=True).first()
    if ref:
        return ref
    run.searches += 1
    found = _call(run, 'search', username, deadline=deadline)
    if found is None:
        return None
    _save(run, found, username)
    return found.ads_ref


def _save(run, item, username):
    """Запись из AdGram → TelegramPeer (новая — «протухшая», чтобы резолвер проверил её сам)"""
    config = run.config
    stale = timezone.now() - TelegramPeer.FRESH_FOR - timedelta(seconds=1)
    peer, created = TelegramPeer.objects.get_or_create(
        username=username, defaults={'peer_type': config['peer_type'], 'fetched_at': stale})
    if created:
        run.created.append(username)
    peer.ads_ref = item.ads_ref
    if item.title:
        peer.title = item.title[:255]
    count_field = config['count_field']
    if getattr(item, count_field) is not None:
        setattr(peer, count_field, getattr(item, count_field))
    peer.save(update_fields=['ads_ref', 'title', count_field])

    if not peer.avatar and item.photo_url:
        run.avatars.append((peer, item.photo_url))


def _save_avatars(run, deadline):
    """Качаем все аватары параллельно (в потоках — только сеть), сохраняем здесь"""
    urls = list(dict.fromkeys(url for _, url in run.avatars))
    # download_cdn_avatar сам проверяет хост (только https://cdn<цифры>.telesco.pe) и ошибки сети
    files = run_parallel(lambda url: download_cdn_avatar(url, deadline), urls, deadline)
    for peer, url in run.avatars:
        avatar = files.get(url)
        if isinstance(avatar, Exception):
            raise avatar  # не сетевая ошибка — баг
        if avatar:
            store_avatar(peer, avatar)
            run.avatars_saved += 1


def _check_in_background(names):
    try:
        refresh_peers.delay(names)
    except Exception as exc:  # брокер недоступен — не страшно: проверим при сохранении объявления
        logger.warning('Не удалось поставить проверку новых каналов из похожих: %s', type(exc).__name__)


def _username(value):
    try:
        return normalize_username(value) if value else None
    except ValueError:
        return None


def parse_usernames(raw, kind):
    """«kunuz,daryo» → ['kunuz', 'daryo']. ValueError с понятным текстом, если что-то не так"""
    config = KINDS[kind]
    names = []
    for part in (raw or '').split(','):
        if not part.strip():
            continue
        try:
            name = normalize_username(part)
        except ValueError:
            raise ValueError(f'Некорректное имя: {part.strip()}') from None
        if name not in names:
            names.append(name)
    if not 1 <= len(names) <= config['max_inputs']:
        raise ValueError(f'Нужно от 1 до {config["max_inputs"]} имён через запятую')
    wrong = not_bots(names) if kind == 'bots' else []
    if wrong:
        raise ValueError(not_bot_error(wrong[0]))
    return names
