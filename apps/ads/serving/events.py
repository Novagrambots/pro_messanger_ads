"""
События показа и клика. Каждое событие обрабатывается отдельно, все Lua-вызовы — одной пачкой в Redis.

view засчитывается, если клиент видел объявление ≥ 1 с и ≥ 50% площади. Lua атомарно: дубль
токена → отказ; зрителю сегодня показали лимит → отказ; иначе списание цены (если есть остаток
и дневной лимит позволяет) или бесплатный показ. Объявление уже не ACTIVE (нет в снимке) —
показ бесплатный. click — бесплатно; повтор того же зрителя по тому же объявлению за 5 с не считается.
"""
import math

from django.utils import timezone

from . import store as S
from . import tokens

MAX_EVENTS = 50
VIEW_MIN_DURATION_MS = 1000
VIEW_MIN_VISIBLE_RATIO = 0.5


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _view_seen_enough(event):
    duration, ratio = event.get('duration_ms'), event.get('visible_ratio')
    return (_number(duration) and duration >= VIEW_MIN_DURATION_MS
            and _number(ratio) and VIEW_MIN_VISIBLE_RATIO <= ratio <= 1)


def _prepare(store, event, viewer, snapshot, day, now):
    """Lua-вызов для события или None, если событие отклоняется без Redis"""
    if not isinstance(event, dict):
        return None
    token = tokens.decode(event.get('random_id'), now)
    if token is None or token.viewer != viewer:  # подделка, просрочен или токен другого зрителя
        return None
    ad_id, ttl = token.ad_id, token.expires_in(now)

    if event.get('type') == 'view':
        if not _view_seen_enough(event):
            return None
        entry = snapshot.ads.get(ad_id)
        if entry is not None:
            limits = [entry.freq_limit, 1, entry.price, entry.daily_limit]
        else:
            limits = [0, 0, 0, -1]  # не ACTIVE — бесплатно и без проверки частоты
        keys = [S.event_key(token.nonce, 'v'), S.freq_key(viewer, day), S.budget_key(ad_id),
                S.daily_key(ad_id, day), S.delta_key(ad_id), S.DIRTY, S.STOP]
        return store.view, keys, [ttl, ad_id, *limits, day, S.FREQ_TTL, S.DAILY_TTL]

    if event.get('type') == 'click':
        keys = [S.event_key(token.nonce, 'c'), S.click_key(ad_id, viewer), S.delta_key(ad_id), S.DIRTY]
        return store.click, keys, [ttl, S.CLICK_WINDOW, day, ad_id]
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
