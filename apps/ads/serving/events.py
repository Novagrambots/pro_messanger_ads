"""
События показа и клика. Каждое событие обрабатывается отдельно, все Lua-вызовы — одной пачкой в Redis.

view клиент шлёт, когда весь текст объявления на экране (как у Telegram); duration_ms и
visible_ratio можно присылать — сервер их не проверяет. Lua атомарно: дубль токена → отказ;
зрителю сегодня показали лимит → отказ; иначе списание цены (если есть остаток и дневной лимит
позволяет) или бесплатный показ. Объявление уже не ACTIVE (нет в снимке) — показ бесплатный.
click — бесплатно; повтор того же зрителя по тому же объявлению за 5 с не считается.
action (подписка на канал / запуск бота после перехода) — один раз на токен и только после
засчитанного клика по нему; video_open (открыл видео объявления) — один раз на токен, только
у объявления с видео. Оба бесплатно.
Оплаченное сообщение в подтверждённом канале партнёрки — ещё и доля партнёру (в том же Lua,
в миллионных сума).
"""
from django.utils import timezone

from apps.ads.models import Placement

from . import store as S
from . import tokens

MAX_EVENTS = 50
# Где показ приносит заработок каналу-партнёру (в видео, ботах и поиске — нет)
PARTNER_PLACEMENTS = (Placement.CHANNEL_MESSAGE, Placement.USERS_CHANNEL_MESSAGE)


def _prepare(store, event, viewer, snapshot, day, now):
    """Lua-вызов для события или None, если событие отклоняется без Redis"""
    if not isinstance(event, dict):
        return None
    token = tokens.decode(event.get('random_id'), now)
    if token is None or token.viewer != viewer:  # подделка, просрочен или токен другого зрителя
        return None
    ad_id, ttl = token.ad_id, token.expires_in(now)

    if event.get('type') == 'view':
        entry = snapshot.ads.get(ad_id)
        partner = ['', 0]
        if entry is not None:
            limits = [entry.freq_limit, 1, entry.price, entry.daily_limit]
            meta = snapshot.channels.get(token.channel_id) if token.channel_id else None
            if meta is not None and meta.partner_permille and entry.placement in PARTNER_PLACEMENTS:
                # Цена в тысячных × доля в тысячных = миллионные сума — точно, без округления
                partner = [token.channel_id, entry.price * meta.partner_permille]
        else:
            limits = [0, 0, 0, -1]  # не ACTIVE — бесплатно и без проверки частоты
        keys = [S.event_key(token.nonce, 'v'), S.freq_key(viewer, day), S.budget_key(ad_id),
                S.daily_key(ad_id, day), S.delta_key(ad_id), S.DIRTY, S.STOP]
        return store.view, keys, [ttl, ad_id, *limits, day, S.FREQ_TTL, S.DAILY_TTL, *partner]

    if event.get('type') == 'click':
        keys = [S.event_key(token.nonce, 'c'), S.click_key(ad_id, viewer), S.delta_key(ad_id), S.DIRTY,
                S.event_key(token.nonce, 'k')]
        return store.click, keys, [ttl, S.CLICK_WINDOW, day, ad_id]

    if event.get('type') == 'action':
        keys = [S.event_key(token.nonce, 'a'), S.event_key(token.nonce, 'k'), S.delta_key(ad_id), S.DIRTY]
        return store.once, keys, [ttl, f'{day}:a', ad_id, '1']

    if event.get('type') == 'video_open':
        entry = snapshot.ads.get(ad_id)
        if entry is not None and (entry.payload['media'] or {}).get('type') != 'VIDEO':
            return None  # у объявления нет видео
        keys = [S.event_key(token.nonce, 'o'), S.event_key(token.nonce, 'k'), S.delta_key(ad_id), S.DIRTY]
        return store.once, keys, [ttl, f'{day}:o', ad_id, '0']
    return None


def process(snapshot, viewer, events, now=None):
    """Возвращает (принято, отклонено)"""
    store = S.store()
    moment = now or timezone.now()
    day, ts = S.day_of(moment), moment.timestamp()
    pipe = store.redis.pipeline(transaction=False)
    queued = rejected = 0
    for event in events:
        call = _prepare(store, event, viewer, snapshot, day, ts)
        if call is None:
            rejected += 1
            continue
        script, keys, args = call
        script(keys=keys, args=args, client=pipe)
        queued += 1
    results = pipe.execute() if queued else []
    accepted = sum(1 for result in results if result > 0)
    return accepted, rejected + queued - accepted
