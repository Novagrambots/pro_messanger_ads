"""
Выбор объявления на запрос рекламы. Запрос ничего не списывает — деньги по событию view.

Клиент просит рекламу для слота (места в приложении), сервер собирает кандидатов из всех
форматов этого слота, как у Telegram: канал — сообщение в канале + Пользователи; бот — бот +
Пользователи; видео — баннер в видео (Пользователи); поиск — поиск.

Подходит: ACTIVE (есть в снимке), расписание разрешает текущий час, остаток бюджета в Redis
≥ цены показа, дневной лимит не исчерпан, зрителю сегодня показали меньше лимита.
Аукцион — один на слот: наибольший CPM, при равенстве — случайный.
"""
import random

from django.utils import timezone

from apps.ads.models import Placement
from apps.directories.models import TelegramPeer

from . import audience
from . import store as S
from .snapshot import allowed_now


USER_ACCOUNT = TelegramPeer.PeerType.USER  # аккаунт пользователя (рекламирует только суперадмин)
# Клиенты, которые показывают в рекламе только канал и бота (Android: AdsManager.resolvePromoted)
NO_USER_ACCOUNT_PLATFORMS = frozenset({'android'})


class Slot:
    """Места в приложении, где клиент просит рекламу"""
    SEARCH = 'SEARCH'
    BOT = 'BOT'
    CHANNEL_MESSAGE = 'CHANNEL_MESSAGE'
    VIDEO = 'VIDEO'
    ALL = (SEARCH, BOT, CHANNEL_MESSAGE, VIDEO)
    IN_CHANNEL = (CHANNEL_MESSAGE, VIDEO)  # контекст — канал (идёт в историю зрителя и в токен)


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


def users_candidates(snapshot, placement, profile, shown_in=None):
    """Формат «Пользователи»: объявления страны зрителя, которым он подходит (audience.matches)"""
    if profile is None or profile.country is None:
        return []
    return [entry for entry in snapshot.users.get(placement, {}).get(profile.country, ())
            if audience.matches(entry.users_rule, profile, shown_in)]


def users_in_channel(snapshot, placement, channel_id, profile):
    """«Пользователи» в канале (сообщение, видео): без рекламы канала в нём самом"""
    shown_in = snapshot.channels.get(channel_id)
    return [entry for entry in users_candidates(snapshot, placement, profile, shown_in)
            if entry.promoted_id != channel_id]


def candidates_for(snapshot, slot, key, profile=None):
    """
    Кандидаты слота: key — нормализованная фраза поиска, имя бота или id канала;
    profile — audience.Viewer (для формата «Пользователи»)
    """
    if slot == Slot.SEARCH:
        return snapshot.search.get(key, ())
    if slot == Slot.BOT:
        # Выбравшие этого бота + «любые боты» + Пользователи; бот, который сам себя продвигает,
        # в своём чате не показывается
        found = (*snapshot.bots.get(key, ()), *snapshot.any_bot,
                 *users_candidates(snapshot, Placement.USERS_BOT_BANNER, profile))
        return [entry for entry in found if entry.promoted_username != key]
    if slot == Slot.CHANNEL_MESSAGE:
        return [*channel_candidates(snapshot, key),
                *users_in_channel(snapshot, Placement.USERS_CHANNEL_MESSAGE, key, profile)]
    if slot == Slot.VIDEO:
        return users_in_channel(snapshot, Placement.USERS_VIDEO_BANNER, key, profile)
    return ()


def pick(snapshot, slot, key, viewer, now=None, profile=None, skip_user_accounts=False):
    """
    Объявление для показа (AdEntry) или None. Один поход в Redis на всех кандидатов.
    skip_user_accounts — клиент не умеет показывать аккаунт пользователя (Android: только канал и бот):
    такие объявления в его аукционе не участвуют, иначе выигравший аккаунт = пустое место у зрителя
    """
    now = now or timezone.now()
    candidates = [entry for entry in candidates_for(snapshot, slot, key, profile)
                  if allowed_now(entry.schedule, now)
                  and not (skip_user_accounts and entry.payload['promoted']['type'] == USER_ACCOUNT)]
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
