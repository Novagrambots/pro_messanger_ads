"""
Мост для старых версий приложения: старые URL выдачи показывают и объявления v2.

Старое приложение не обновить: оно ходит в /api/search_ads/order/search/, /api/chat_ads/order/search/
и …/order/click/ (ключ зашит в приложение) и не знает про random_id и события. Мост переводит старый
запрос в выдачу v2 — тот же снимок, аукцион и Lua-скрипты. Поэтому бюджет, дневной лимит, частота
на зрителя, статистика и заработок партнёра у старых и новых версий общие.

Отличия старого протокола:
- выдача = показ: старый клиент не шлёт view, поэтому VIEW_LUA исполняется сразу при выдаче;
- клик приходит как order_id + viewer_id без токена: токен выдачи лежит в Redis (store.legacy_key)
  столько же, сколько живёт random_id. Без выдачи этому зрителю клик по объявлению v2 не засчитывается;
- объявления v2 — только зрителю с числовым viewer_id (id Telegram → тот же viewer_key, что у новых версий);
- старые заказы, пока они крутятся, участвуют в том же аукционе по цене (spm и cpm — сумы за 1000 показов).
  Где их нет (LegacyIndex в снимке), в Postgres не ходим.

Лимиты запросов v2 (на зрителя и на IP) действуют и на старые URL. Превышен лимит — старый клиент получает
обычное «рекламы нет» (404), клик молча не считается: ответов, которых старый клиент не знает, не шлём.
Мост выключен (ADS_LEGACY_BRIDGE=False), нет секретов выдачи или Redis недоступен — старые URL работают
как раньше (только старые заказы).
"""
import logging
import time
import uuid
from dataclasses import dataclass

import redis
from django.conf import settings
from django.db import DatabaseError
from django.utils import timezone

from apps.ads.models import AdMedia
from apps.chat_ads.models import ChatAdOrder
from apps.common.models import ModerationStatus
from apps.search_ads.models import Order

from . import events as ad_events
from . import geo, selection, snapshot
from . import store as S
from . import tokens
from .selection import Slot
from .views import CHANNEL_ID_RE, viewer_profile

logger = logging.getLogger(__name__)

LIMITED = 'limited'    # превышен лимит запросов — старому клиенту «рекламы нет»
PLATFORM = 'TELEGRAM'  # объявления v2 продвигают каналы и ботов Telegram (ссылка t.me)
# Тип медиа v2 → как его называл старый ChatAdMedia
MEDIA_TYPES = {AdMedia.MediaType.PHOTO: 'IMAGE', AdMedia.MediaType.VIDEO: 'VIDEO'}
# Старый заказ ещё крутится (те же условия, что у старых вьюх выдачи)
LIVE_ORDER = {'status': ModerationStatus.APPROVED, 'is_active': True, 'cancelled': False, 'remaining_views__gt': 0}
_last_outage_log = 0.0


def enabled():
    return settings.ADS_LEGACY_BRIDGE and tokens.is_configured()


def _log_outage(message):
    """Redis / БД недоступны: в лог не чаще раза в 10 с на процесс"""
    global _last_outage_log
    if time.monotonic() - _last_outage_log > 10:
        _last_outage_log = time.monotonic()
        logger.exception(message)


# ===================================== Индекс старых заказов =====================================

@dataclass(frozen=True)
class LegacyIndex:
    """Где ещё крутятся старые заказы. Перечитывается вместе со снимком (раз в 30 с и при их сохранении)"""
    search_tags: frozenset = frozenset()
    chat_channels: frozenset = frozenset()
    chat_anywhere: bool = False  # есть заказ в чатах без таргетинга — подходит любому каналу


def load_index():
    """Два запроса: теги живых заказов поиска и каналы живых заказов в чатах (вызывает snapshot.load)"""
    tags = Order.objects.filter(**LIVE_ORDER).values_list('tags__name', flat=True).distinct()
    channels, anywhere = set(), False
    for value in ChatAdOrder.objects.filter(completed=False, **LIVE_ORDER).values_list('channels', flat=True).distinct():
        anywhere = anywhere or value == ''
        channels.update(name.strip() for name in value.split(',') if name.strip())
    return LegacyIndex(frozenset(tag for tag in tags if tag), frozenset(channels), anywhere)


def order_saved(sender, instance, update_fields=None, **kwargs):
    """
    post_save старого заказа: снимок (и индекс) перечитать сразу — заказ одобрили, отменили, включили.
    Клик сохраняет только clicks / updated_at — от него индекс не меняется, версию снимка не трогаем
    """
    if not settings.ADS_LEGACY_BRIDGE:
        return
    if update_fields is not None and set(update_fields) <= {'clicks', 'updated_at'}:
        return
    snapshot.ad_saved(sender, instance)


# ========================================= Выдача =========================================

@dataclass(frozen=True)
class Offer:
    """Итог проверки старого запроса: снимок и лучшее объявление v2 (или None)"""
    snapshot: object
    viewer: str = None    # viewer_key; None — viewer_id не числовой (объявлений v2 нет)
    slot: str = None
    key: object = None    # нормализованная фраза поиска или id канала
    entry: object = None  # snapshot.AdEntry


@dataclass(frozen=True)
class Shown:
    """Объявление v2, показ которого уже засчитан"""
    entry: object
    remaining_views: int  # сколько показов по текущей цене ещё оплачено (остаток в Redis)


def _viewer(viewer_id):
    """viewer_key как у новых версий или None (viewer_id — не числовой id Telegram)"""
    user_id = tokens.parse_user_id(viewer_id)
    return tokens.viewer_key(user_id) if user_id is not None else None


def _rate_ident(viewer, viewer_id):
    """Кого считает лимит: числовой зритель — общий счётчик с новыми версиями, прочие — по хэшу старого id"""
    return viewer or tokens.viewer_key(f'legacy:{viewer_id}')


def offer_search(request, tag, viewer_id):
    """Старый поиск: тег — это фраза поиска v2"""
    return _offer(request, viewer_id, Slot.SEARCH, snapshot.normalize_query(tag) or None)


def offer_chat(request, channel_id, viewer_id):
    """Старая выдача в чатах = сообщение в канале v2. Без id канала в формате -100… объявлений v2 нет"""
    key = int(channel_id) if channel_id is not None and CHANNEL_ID_RE.fullmatch(str(channel_id)) else None
    return _offer(request, viewer_id, Slot.CHANNEL_MESSAGE, key)


def _offer(request, viewer_id, slot, key):
    """
    None — мост выключен или Redis / база недоступны (старый путь как раньше); LIMITED — лимит запросов;
    иначе Offer (entry=None — подходящего объявления v2 нет). Два похода в Redis, Postgres не нужен
    """
    if not enabled():
        return None
    viewer = _viewer(viewer_id)
    in_channel = viewer is not None and key is not None and slot in Slot.IN_CHANNEL
    try:
        limited, history = S.rate_limit_and_history(
            [('serve', _rate_ident(viewer, viewer_id), settings.ADS_RATE_VIEWER_SERVE),
             ('ip', geo.client_ip(request), settings.ADS_RATE_IP)],
            viewer, add_channel=key if in_channel else None, read_history=in_channel)
        if limited:
            return LIMITED
        snap = snapshot.current()
        entry = None
        if viewer is not None and key is not None:
            # Старый клиент не шлёт язык и страну телефона — для «Пользователей» страна и город по IP
            profile = viewer_profile(snap, {}, request, history) if slot != Slot.SEARCH else None
            entry = selection.pick(snap, slot, key, viewer, profile=profile)
    except (redis.RedisError, DatabaseError):
        _log_outage('Мост старых версий: выдача v2 недоступна — работают только старые заказы')
        return None
    return Offer(snap, viewer, slot, key, entry)


def _index(offer):
    return getattr(offer.snapshot, 'legacy', None) if isinstance(offer, Offer) else None


def may_have_search_orders(offer, tag):
    """Искать ли старые заказы поиска в Postgres. Индекса нет (мост выключен, Redis лежит) — ищем, как раньше"""
    index = _index(offer)
    return index is None or tag in index.search_tags


def may_have_chat_orders(offer, channel_name):
    """Искать ли старые заказы в чатах в Postgres"""
    index = _index(offer)
    return index is None or index.chat_anywhere or channel_name in index.chat_channels


def run(request, offer, orders, show_order):
    """
    Общий аукцион старого URL: старые заказы (по убыванию spm) и лучшее объявление v2 — по цене за 1000
    показов, при равной цене — v2. show_order(order) показывает старый заказ (данные ответа или None).
    Возвращает (данные старого заказа, None), (None, Shown) или (None, None) — рекламы нет
    """
    entry = offer.entry if isinstance(offer, Offer) else None
    for order in orders:
        if entry is not None and entry.price >= order.spm:
            shown = commit(offer)
            if shown is not None:
                return None, shown
            entry = None  # показ не прошёл (частота, Redis) — дальше только старые заказы
        data = show_order(order)
        if data is not None:
            return data, None
    if entry is not None:
        shown = commit(offer)
        if shown is not None:
            return None, shown
    return None, None


def commit(offer):
    """
    Засчитать показ offer.entry сейчас (старый протокол: выдача = показ) тем же VIEW_LUA, что и событие
    view новых версий, и запомнить токен для клика. Один поход в Redis. None — показ не прошёл
    """
    entry, viewer = offer.entry, offer.viewer
    random_id = tokens.issue(entry.id, viewer, entry.placement, tokens.context_hash(offer.slot, offer.key),
                             channel_id=offer.key if offer.slot in Slot.IN_CHANNEL else None)
    moment = timezone.now()
    store = S.store()
    call = ad_events.prepare(store, {'type': 'view', 'random_id': random_id}, viewer, offer.snapshot,
                             S.day_of(moment), moment.timestamp())
    if call is None:
        return None
    script, keys, args = call
    pipe = store.redis.pipeline(transaction=False)
    pipe.set(S.legacy_key(viewer, entry.id), random_id, ex=settings.ADS_TOKEN_TTL)
    script(keys=keys, args=args, client=pipe)
    pipe.get(S.budget_key(entry.id))
    try:
        _, result, left = pipe.execute()
    except redis.RedisError:
        _log_outage('Мост старых версий: показ объявления v2 не записан')
        return None
    # 1 — оплачен, 2 — бесплатный (бюджет кончился за эти миллисекунды): показ записан, отдаём.
    # -1 — зрителю сегодня уже показали лимит (параллельный запрос успел раньше): не показываем
    if result <= 0:
        return None
    return Shown(entry, int(left) // entry.price if left else 0)


# ========================================== Клик ==========================================

def click(request, data):
    """
    Клик старого клиента (тело {order_id, viewer_id}). True — разобрались здесь (клик объявления v2
    засчитан или отброшен как повтор, или превышен лимит), False — пусть разбирается старый путь
    (старые заказы и ответы об ошибках — как раньше)
    """
    if not enabled() or not isinstance(data, dict):
        return False
    order_id, viewer_id = data.get('order_id'), data.get('viewer_id')
    if not isinstance(order_id, str) or not isinstance(viewer_id, (str, int)) or isinstance(viewer_id, bool):
        return False
    try:
        ad_id = str(uuid.UUID(order_id))
    except ValueError:
        return False
    viewer = _viewer(viewer_id)
    try:
        if S.over_rate_limit([('events', _rate_ident(viewer, viewer_id), settings.ADS_RATE_VIEWER_EVENTS),
                              ('ip', geo.client_ip(request), settings.ADS_RATE_IP)]):
            return True
        if viewer is None:
            return False
        random_id = S.store().redis.get(S.legacy_key(viewer, ad_id))
        if random_id is None:
            return False  # этому зрителю объявление v2 через старый URL не выдавали — не наш клик
        # Тот же CLICK_LUA: повтор того же зрителя за 5 с и второй клик по одной выдаче не считаются
        ad_events.process(snapshot.current(), viewer, [{'type': 'click', 'random_id': random_id}])
    except (redis.RedisError, DatabaseError):
        _log_outage('Мост старых версий: клик по объявлению v2 не записан')
        return False
    return True


# ===================================== Ответы старого формата =====================================

def search_result(shown):
    """Ответ старого поиска (SearchResultSerializer) для объявления v2"""
    promoted = shown.entry.payload['promoted']
    return {
        'channel_id': str(promoted['telegram_id'] or promoted['username']),
        'channel_name': promoted['username'],
        'order_id': shown.entry.id,
        'platform': PLATFORM,
    }


def chat_result(request, shown):
    """Ответ старой выдачи в чатах (ChatAdSearchSerializer) для объявления v2"""
    payload = shown.entry.payload
    promoted, media = payload['promoted'], payload['media']
    return {
        'order_id': shown.entry.id,
        # Название объявления v2 — внутреннее (для кабинета), зрителю — название продвигаемого канала
        'order_name': promoted['title'] or promoted['username'],
        'text': payload['text'],
        'link': payload['url'],
        'platform': PLATFORM,
        'media_url': request.build_absolute_uri(media['url']) if media else None,
        'media_type': MEDIA_TYPES.get(media['type']) if media else None,
        # Точные итоги объявления v2 — в кабинете; здесь только остаток оплаченных показов из Redis
        'total_views': shown.remaining_views,
        'clicks': 0,
        'shown_views': 0,
        'remaining_views': shown.remaining_views,
    }
