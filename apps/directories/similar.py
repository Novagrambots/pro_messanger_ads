"""
Похожие каналы и боты — через внутреннее API AdGram (оно ходит на ads.telegram.org).

- ads_ref — непрозрачный хэш ads.telegram.org: только для запросов похожих, это НЕ telegram_id
  и в таргетинг не попадает. Запоминаем навсегда (обновляем, если AdGram вернул другой).
- Данные из AdGram не считаются проверенными резолвером: новые записи TelegramPeer создаём
  «протухшими» — при добавлении в таргетинг резолвер сходит в Telegram за числовым id.
  Существующим записям обновляем только ads_ref, название, счётчики и пустой аватар.
- Ответ кэшируем на 7 дней по набору входных имён (в кэше — только имена, сам ответ
  собирается из TelegramPeer, чтобы аватары и названия были свежими).
- AdGram недоступен / 429 / 503 → SimilarUnavailable (фронт спрячет кнопку). После 429 —
  «остываем» по Retry-After (не дольше 10 минут), чтобы не долбить AdGram и Telegram.
"""
import hashlib
import logging
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from apps.billing import adgram_client
from apps.billing.adgram_client import AdGramNotConfigured, AdGramRejected, AdGramUnavailable
from apps.directories.models import TelegramPeer
from apps.directories.telegram import (Deadline, DeadlineExceeded, download_cdn_avatar, normalize_username,
                                       store_avatar)

logger = logging.getLogger(__name__)

CACHE_TTL = 7 * 24 * 60 * 60
COOLDOWN_KEY = 'similar:cooldown'
MAX_COOLDOWN = 10 * 60
REQUEST_SECONDS = 20  # общий лимит времени на запрос
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
        names = _fetch(KINDS[kind], usernames)
        cache.set(key, names, CACHE_TTL)
    peers = {peer.username: peer for peer in TelegramPeer.objects.filter(username__in=names)}
    return [peers[name] for name in names if name in peers]


def _call(config, name, *args, deadline):
    timeout = deadline.timeout(ADGRAM_READ_TIMEOUT)
    return getattr(adgram_client, config[name])(*args, timeout=(min(2, timeout), timeout))


def _fetch(config, usernames):
    if not adgram_client.is_configured() or cache.get(COOLDOWN_KEY):
        raise SimilarUnavailable()

    deadline = Deadline(REQUEST_SECONDS)
    try:
        refs = [ref for ref in (_ads_ref(config, name, deadline) for name in usernames) if ref]
        results = _call(config, 'similar', refs, deadline=deadline) if refs else []
    except AdGramUnavailable as exc:
        if exc.retry_after:
            cache.set(COOLDOWN_KEY, True, min(exc.retry_after, MAX_COOLDOWN))
        raise SimilarUnavailable() from None
    except (AdGramRejected, AdGramNotConfigured) as exc:
        logger.error('AdGram: запрос похожих отклонён: %s', exc)  # 401 / 400 — ошибка настройки или наш баг
        raise SimilarUnavailable() from None
    except DeadlineExceeded:
        raise SimilarUnavailable() from None

    names, inputs = [], set(usernames)
    for item in results:
        username = _username(item.username)
        if username and username not in inputs and username not in names:
            _save(config, item, username, deadline)
            names.append(username)
    return names


def _ads_ref(config, username, deadline):
    """ads_ref имени: из базы, иначе поиском в AdGram (и запоминаем). None — нет на ads.telegram.org"""
    ref = TelegramPeer.objects.filter(username=username).values_list('ads_ref', flat=True).first()
    if ref:
        return ref
    found = _call(config, 'search', username, deadline=deadline)
    if found is None:
        return None
    _save(config, found, username, deadline)
    return found.ads_ref


def _save(config, item, username, deadline):
    """Запись из AdGram → TelegramPeer (новая — «протухшая», чтобы резолвер проверил её сам)"""
    stale = timezone.now() - TelegramPeer.FRESH_FOR - timedelta(seconds=1)
    peer, _ = TelegramPeer.objects.get_or_create(
        username=username, defaults={'peer_type': config['peer_type'], 'fetched_at': stale})
    peer.ads_ref = item.ads_ref
    if item.title:
        peer.title = item.title[:255]
    count_field = config['count_field']
    if getattr(item, count_field) is not None:
        setattr(peer, count_field, getattr(item, count_field))
    peer.save(update_fields=['ads_ref', 'title', count_field])

    if not peer.avatar and item.photo_url:
        avatar = download_cdn_avatar(item.photo_url, deadline)  # только https://cdn<цифры>.telesco.pe
        if avatar:
            store_avatar(peer, avatar)


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
        if kind == 'bots' and not name.endswith('bot'):
            raise ValueError(f'@{name} — не бот: имя бота кончается на «bot»')
        if name not in names:
            names.append(name)
    if not 1 <= len(names) <= config['max_inputs']:
        raise ValueError(f'Нужно от 1 до {config["max_inputs"]} имён через запятую')
    return names
