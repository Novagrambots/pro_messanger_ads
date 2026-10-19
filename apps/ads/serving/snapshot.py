"""
Снимок ACTIVE-объявлений в памяти процесса: запрос рекламы и события не ходят в Postgres.
Для сообщений в каналах — ещё язык, темы и доля партнёра каждого канала из партнёрки;
для формата «Пользователи» — справочники стран, языков и локаций и тайминги баннера в видео.

Фоновый поток раз в секунду сверяет версию в Redis (её повышает любое сохранение Ad) и
перечитывает снимок при смене версии и в любом случае раз в 30 с. Самый первый запрос
процесса загружает снимок сам. При загрузке недостающие ключи бюджета создаются в Redis
из Postgres (SET NX) — кроме «замороженных» на время возврата или пополнения.
"""
import logging
import os
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from zoneinfo import ZoneInfo

import redis
from django.conf import settings
from django.db import close_old_connections, transaction
from django.db.models import Q

from apps.ads.models import USERS_PLACEMENTS, Ad, AdMedia, AdPricing, AdStatus, Placement
from apps.directories.models import Country, Language, Location, TelegramPeer
from apps.partner.models import ChannelEarning

from . import store as S
from .audience import LocationIndex, users_rule

logger = logging.getLogger(__name__)

CHECK_EVERY = 1.0    # проверка версии, с
RELOAD_EVERY = 30.0  # перечитать в любом случае, с
_SETNX_CHUNK = 1000  # пар «объявление, остаток» в одном вызове Lua

_APOSTROPHES = str.maketrans({c: "'" for c in 'ʻʼ’‘`´'})


def normalize_query(text):
    """Фраза поиска: нижний регистр, апострофы ʻ ’ ‘ ` ʼ ´ → ', пробелы схлопнуты, края обрезаны"""
    return ' '.join(text.translate(_APOSTROPHES).lower().split())


@dataclass(frozen=True)
class ChannelRule:
    """Таргетинг сообщения в канале: явный список каналов ИЛИ языки + темы (id каналов — как в Bot API)"""
    channels: frozenset
    languages: frozenset
    topics: frozenset
    exclude_topics: frozenset
    exclude_channels: frozenset


@dataclass(frozen=True)
class ChannelMeta:
    """Канал из партнёрки: язык, темы и доля партнёра в тысячных (0 — канал не подтверждён)"""
    language: str
    topics: frozenset
    partner_permille: int


@dataclass(frozen=True)
class AdEntry:
    id: str
    placement: str
    price: int        # цена показа в тысячных сума (= cpm)
    freq_limit: int   # показов одному зрителю в сутки, 0 — без ограничений
    daily_limit: int  # дневной лимит в тысячных сума, -1 — без лимита
    schedule: tuple   # (часовой пояс, 7 масок часов) или None — круглосуточно
    payload: dict     # публичная часть ответа (без random_id); ссылки на файлы — относительные
    promoted_username: str  # что продвигаем (нижний регистр) — свой бот в своём чате не показывается
    promoted_id: int        # id продвигаемого канала — реклама канала не показывается в нём самом
    channel_rule: ChannelRule = None  # только у сообщения в канале
    users_rule: object = None         # audience.UsersRule — только у формата «Пользователи»


@dataclass(frozen=True)
class World:
    """Справочники для профиля зрителя и настройки показа"""
    countries: frozenset = frozenset()
    languages: frozenset = frozenset()
    locations: LocationIndex = LocationIndex([])
    video: dict = None  # тайминги баннера в видео (start_delay, between_delay, min/max_display_duration)


def schedule_rule(schedule):
    if not schedule or not schedule.get('enabled'):
        return None
    zone = schedule.get('timezone') or settings.TIME_ZONE
    if zone == 'viewer':  # пояс зрителя (места показа Пользователей) — до этапа 7г считаем по Ташкенту
        zone = settings.TIME_ZONE
    return ZoneInfo(zone), tuple(schedule['days'])


def allowed_now(rule, now):
    """Расписание разрешает текущий час (маски Telegram: бит h — час h:00–h+1:00, Пн…Вс)"""
    if rule is None:
        return True
    zone, days = rule
    local = now.astimezone(zone)
    return bool(days[local.weekday()] >> local.hour & 1)


def _promoted(ad):
    """Что продвигаем. У объявлений до этапа 5а нет promoted_peer — берём имя из ссылки"""
    peer = ad.promoted_peer
    username, peer_type = ad.promoted_username, ad.promoted_type
    # В поиске карточка всегда с аватаром; в остальных местах — если выбрано «фото канала»
    show_avatar = ad.placement == Placement.SEARCH or ad.show_channel_picture
    return {
        'username': username,
        'title': peer.title if peer else '',
        'avatar_url': peer.avatar.url if peer and peer.avatar and show_avatar else None,
        'telegram_id': peer.telegram_id if peer else None,
        'type': peer_type,
    }


def _media(ad):
    media = ad.media
    if media is None or media.status != AdMedia.Status.READY:
        return None
    return {
        'type': media.media_type,
        'url': media.file.url,
        'width': media.width,
        'height': media.height,
        'duration': str(media.duration) if media.duration is not None else None,
    }


def _channel_ids(items):
    # Каналы без числового id (резолвер без токена бота) ни с чем не совпадут
    return frozenset(item['id'] for item in items or [] if item.get('id'))


def channel_rule(targeting):
    return ChannelRule(
        channels=_channel_ids(targeting.get('channels')),
        languages=frozenset(targeting.get('languages') or []),
        topics=frozenset(targeting.get('topics') or []),
        exclude_topics=frozenset(targeting.get('exclude_topics') or []),
        exclude_channels=_channel_ids(targeting.get('exclude_channels')),
    )


def share_permille(rate):
    """Доля партнёра в тысячных: 0.5 → 500. Больше 3 знаков — ошибка в лог и вниз до 3 знаков"""
    value = Decimal(str(rate)) * 1000
    whole = int(value)
    if value != whole:
        logger.error('Доля партнёра %s — больше 3 знаков после запятой, считаем %s', rate, Decimal(whole) / 1000)
    return max(0, min(whole, 1000))


def make_entry(ad, world=None):
    promoted = _promoted(ad)
    payload = {
        'placement': ad.placement,
        'text': ad.text,
        'url': ad.url,
        'promoted': promoted,
        'button_type': promoted['type'],  # CHANNEL / BOT — текст кнопки переводит клиент
        'media': _media(ad),
        'other_info': ad.other_info,
        'label': 'Ad',
    }
    return AdEntry(
        id=str(ad.pk),
        placement=ad.placement,
        price=ad.cpm,
        freq_limit=ad.views_per_user_per_day,
        daily_limit=ad.daily_limit * 1000 if ad.daily_limit else -1,
        schedule=schedule_rule(ad.schedule),
        payload=payload,
        promoted_username=promoted['username'].lower(),
        promoted_id=promoted['telegram_id'] if promoted['type'] == TelegramPeer.PeerType.CHANNEL else None,
        channel_rule=channel_rule(ad.targeting or {}) if ad.placement == Placement.CHANNEL_MESSAGE else None,
        users_rule=(users_rule(ad.targeting or {}, (world or World()).locations.country)
                    if ad.placement in USERS_PLACEMENTS else None),
    )


def _by_price(entries):
    return tuple(sorted(entries, key=lambda e: -e.price))


class Snapshot:
    def __init__(self, ads, version, channels=None, world=None):
        self.version = version
        self.loaded_at = time.monotonic()
        self.ads = {}
        self.channels = channels or {}  # channel_id → ChannelMeta
        self.world = world or World()
        search, bots, any_bot, channel_ads, lang_ads, users = {}, {}, [], {}, {}, {}
        for ad in ads:
            entry = make_entry(ad, self.world)
            self.ads[entry.id] = entry
            targeting = ad.targeting or {}
            if ad.placement == Placement.SEARCH:
                for phrase in {normalize_query(q) for q in targeting.get('queries', [])}:
                    search.setdefault(phrase, []).append(entry)
            elif ad.placement == Placement.BOT:
                names = {name.lower() for name in targeting.get('bots') or []}
                for name in names:
                    bots.setdefault(name, []).append(entry)
                if not names:
                    any_bot.append(entry)  # боты не выбраны — показываем в любых
            elif ad.placement == Placement.CHANNEL_MESSAGE:
                rule = entry.channel_rule
                if rule.channels:  # режим «список каналов»
                    for channel_id in rule.channels:
                        channel_ads.setdefault(channel_id, []).append(entry)
                else:  # режим «языки + темы»
                    for language in rule.languages:
                        lang_ads.setdefault(language, []).append(entry)
            elif ad.placement in USERS_PLACEMENTS:
                for country in entry.users_rule.countries:  # страна обязательна — индекс по ней
                    users.setdefault(ad.placement, {}).setdefault(country, []).append(entry)
        # Фраза / имя бота / канал / язык → объявления по убыванию ставки
        self.search = {phrase: _by_price(entries) for phrase, entries in search.items()}
        self.bots = {name: _by_price(entries) for name, entries in bots.items()}
        self.any_bot = _by_price(any_bot)
        self.channel_ads = {channel_id: _by_price(entries) for channel_id, entries in channel_ads.items()}
        self.lang_ads = {language: _by_price(entries) for language, entries in lang_ads.items()}
        # Формат «Пользователи» → страна → объявления
        self.users = {placement: {country: _by_price(entries) for country, entries in by_country.items()}
                      for placement, by_country in users.items()}


def load_channels():
    """Каналы партнёрки, нужные выдаче: с языком (режим «языки + темы») или подтверждённые (заработок)"""
    confirmed = ChannelEarning.ClaimStatus.CONFIRMED
    default = share_permille(settings.PARTNER_SHARE_RATE)
    rows = ChannelEarning.objects.filter(Q(language__isnull=False) | Q(claim_status=confirmed)).values_list(
        'channel_id', 'language_id', 'topics', 'claim_status', 'owner_id', 'share_rate')
    channels = {}
    for channel_id, language, topics, claim_status, owner_id, rate in rows:
        partner = claim_status == confirmed and owner_id is not None
        permille = (share_permille(rate) if rate is not None else default) if partner else 0
        channels[channel_id] = ChannelMeta(language, frozenset(topics or []), permille)
    return channels


def load_world():
    """Активные страны и языки, все локации (для города зрителя), тайминги видео из «Цен и правил»"""
    pricing = AdPricing.load()
    locations = [
        (location.pk, location.country_id, [location.name_en, location.name_ru, location.name_uz],
         [location.region_en, location.region_ru, location.region_uz])
        for location in Location.objects.all()
    ]
    return World(
        countries=frozenset(Country.objects.filter(is_active=True).values_list('code', flat=True)),
        languages=frozenset(Language.objects.filter(is_active=True).values_list('code', flat=True)),
        locations=LocationIndex(locations),
        video={'start_delay': pricing.video_start_delay, 'between_delay': pricing.video_between_delay,
               'min_display_duration': pricing.video_min_display, 'max_display_duration': pricing.video_max_display},
    )


def load():
    """Читает ACTIVE-объявления из Postgres и создаёт недостающие ключи бюджета в Redis"""
    s = S.store()
    # Версию читаем ДО запроса в БД: изменения во время загрузки поймает следующая проверка
    version = s.redis.get(S.VERSION)
    ads = list(Ad.objects.filter(status=AdStatus.ACTIVE).select_related('promoted_peer', 'media'))
    pairs = []
    for ad in ads:
        pairs += [str(ad.pk), S.to_thousandths(ad.remaining_budget())]
    for start in range(0, len(pairs), _SETNX_CHUNK):
        s.setnx_budgets(args=[S.PREFIX, *pairs[start:start + _SETNX_CHUNK]])
    return Snapshot(ads, version, load_channels(), load_world())


_current = None
_load_lock = threading.Lock()
_thread_pid = None


def current():
    """Снимок для запроса. Бросает ошибку Redis / БД, только если снимка ещё нет совсем"""
    snapshot = _current
    if snapshot is None:
        with _load_lock:
            snapshot = _current or reload()
    if settings.ADS_SNAPSHOT_THREAD:
        _ensure_thread()
    return snapshot


def reload():
    global _current
    _current = load()
    return _current


def refresh_if_needed():
    """Одна итерация фонового потока: версия сменилась или снимку 30 с — перечитать"""
    snapshot = _current
    version = S.store().redis.get(S.VERSION)
    if snapshot is None or version != snapshot.version or time.monotonic() - snapshot.loaded_at >= RELOAD_EVERY:
        reload()


def _run():
    last_error = 0.0
    while True:
        time.sleep(CHECK_EVERY)
        try:
            close_old_connections()
            refresh_if_needed()
        except Exception:
            # Работаем на прежнем снимке; в лог — не чаще раза в минуту
            if time.monotonic() - last_error > 60:
                logger.exception('Снимок объявлений не обновился — работаем на прежнем')
                last_error = time.monotonic()


def _ensure_thread():
    """Поток — свой в каждом процессе (после fork потоки родителя не живут)"""
    global _thread_pid
    if _thread_pid == os.getpid():
        return
    with _load_lock:
        if _thread_pid != os.getpid():
            threading.Thread(target=_run, name='ads-snapshot', daemon=True).start()
            _thread_pid = os.getpid()


# ================================ Версия снимка ================================

def ad_saved(sender, instance, **kwargs):
    """post_save объявления: после коммита повысить версию — снимки во всех процессах перечитаются"""
    transaction.on_commit(_bump_quietly)


def _bump_quietly():
    try:
        S.bump_version()
    except redis.RedisError:
        logger.warning('Не удалось повысить версию снимка объявлений — перечитается по таймеру (30 с)')
