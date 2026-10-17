"""
Выбор объявления на запрос рекламы. Запрос ничего не списывает — деньги по событию view.

Подходит: ACTIVE (есть в снимке), расписание разрешает текущий час, остаток бюджета в Redis
≥ цены показа, дневной лимит не исчерпан, зрителю сегодня показали меньше лимита.
Аукцион: наибольший CPM, при равенстве — случайный.
"""
import random

from django.utils import timezone

from apps.ads.models import Placement

from . import store as S
from .snapshot import allowed_now


def channel_candidates(snapshot, channel_id):
    """
    Сообщение в канале: объявления, выбравшие канал явно, + режим «языки + темы» — язык канала
    в языках объявления, темы пересекаются (если заданы) и не попадают в исключённые. Канал без
    языка в режим «языки + темы» не попадает. Для обоих режимов — без исключённых каналов
    и без рекламы канала в нём самом.
    """
    found = list(snapshot.channel_ads.get(channel_id, ()))
    meta = snapshot.channels.get(channel_id)
    if meta is not None and meta.language:
        for entry in snapshot.lang_ads.get(meta.language, ()):
            rule = entry.channel_rule
            if rule.topics and not rule.topics & meta.topics:
                continue
            if rule.exclude_topics & meta.topics:
                continue
            found.append(entry)
    return [entry for entry in found
            if channel_id not in entry.channel_rule.exclude_channels and entry.promoted_id != channel_id]


def candidates_for(snapshot, placement, key):
    """Объявления по контексту запроса: key — нормализованная фраза поиска, имя бота или id канала"""
    if placement == Placement.SEARCH:
        return snapshot.search.get(key, ())
    if placement == Placement.BOT:
        # Выбравшие этого бота + «любые боты»; бот, который сам себя продвигает, в своём чате не показывается
        return [entry for entry in (*snapshot.bots.get(key, ()), *snapshot.any_bot) if entry.promoted_username != key]
    if placement == Placement.CHANNEL_MESSAGE:
        return channel_candidates(snapshot, key)
    return ()


def pick(snapshot, placement, key, viewer, now=None):
    """Объявление для показа (AdEntry) или None. Один поход в Redis на всех кандидатов"""
    now = now or timezone.now()
    candidates = [entry for entry in candidates_for(snapshot, placement, key) if allowed_now(entry.schedule, now)]
    if not candidates:
        return None

    day = S.day_of(now)
    pipe = S.store().redis.pipeline(transaction=False)
    pipe.mget([S.budget_key(entry.id) for entry in candidates])
    pipe.mget([S.daily_key(entry.id, day) for entry in candidates])
    pipe.hmget(S.freq_key(viewer, day), [entry.id for entry in candidates])
    budgets, dailies, seen = pipe.execute()

    best, best_price = [], None
    for entry, budget, daily, count in zip(candidates, budgets, dailies, seen):
        if budget is None or int(budget) < entry.price:
            continue
        if entry.daily_limit >= 0 and int(daily or 0) + entry.price > entry.daily_limit:
            continue
        if entry.freq_limit and int(count or 0) >= entry.freq_limit:
            continue
        if best_price is None or entry.price > best_price:
            best, best_price = [entry], entry.price
        elif entry.price == best_price:
            best.append(entry)
    return random.choice(best) if best else None
