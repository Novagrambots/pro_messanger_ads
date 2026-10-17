"""
Формат «Пользователи» (Users): профиль зрителя и правила таргетинга, как у Telegram —
внутри одного списка ИЛИ, между измерениями И, исключения вычитаются.

Профиль считается на сервере и нигде не хранится: страна (phone_country, иначе по IP),
локации (город по IP + его область + регион из гео-базы), язык, устройство, история каналов
зрителя (Redis, 30 дней) и темы интересов — объединение тем этих каналов.
"""
import re
from dataclasses import dataclass

POLITICS_TOPIC = 37  # «Politics & Incidents»

# platform клиента → устройство из таргетинга (validators.DEVICES)
DEVICE_ALIASES = {
    'ios': 'ios', 'iphone': 'ios', 'ipad': 'ios',
    'android': 'android',
    'desktop': 'desktop', 'windows': 'desktop', 'macos': 'desktop', 'linux': 'desktop', 'web': 'desktop',
}

_APOSTROPHES = re.compile(r"[ʻʼ’‘`´']")
_REGION_WORDS = re.compile(r'\b(region|province|viloyati|oblast|область)\b')


@dataclass(frozen=True)
class UsersRule:
    countries: frozenset
    locations: dict          # страна → frozenset id локаций (только страны, где локации выбраны)
    languages: frozenset
    topics: frozenset
    exclude_topics: frozenset
    audiences: frozenset     # id каналов (Bot API)
    exclude_audiences: frozenset
    devices: frozenset
    politics: str            # 'only' | 'exclude' | None


@dataclass(frozen=True)
class Viewer:
    country: str = None
    locations: frozenset = frozenset()
    language: str = None
    device: str = None
    channels: frozenset = frozenset()  # история каналов зрителя
    topics: frozenset = frozenset()    # темы интересов


def _channel_ids(items):
    return frozenset(item['id'] for item in items or [] if item.get('id'))


def users_rule(targeting, location_country):
    """Правило из таргетинга объявления; location_country — {id локации: страна}"""
    locations = {}
    for location_id in targeting.get('locations') or []:
        country = location_country.get(location_id)
        if country:
            locations.setdefault(country, set()).add(location_id)
    return UsersRule(
        countries=frozenset(targeting.get('countries') or []),
        locations={country: frozenset(ids) for country, ids in locations.items()},
        languages=frozenset(targeting.get('languages') or []),
        topics=frozenset(targeting.get('topics') or []),
        exclude_topics=frozenset(targeting.get('exclude_topics') or []),
        audiences=_channel_ids(targeting.get('channel_audiences')),
        exclude_audiences=_channel_ids(targeting.get('exclude_channel_audiences')),
        devices=frozenset(targeting.get('devices') or []),
        politics=targeting.get('politics'),
    )


def matches(rule, viewer, shown_in=None):
    """
    Подходит ли зритель. shown_in — ChannelMeta канала, где показ (для галочки политики), или None.
    Галочка «только политика» — у канала есть тема 37; «без политики» — темы канала известны
    и темы 37 среди них нет (канал без тем — не показываем: безопасность бренда).
    """
    if viewer.country not in rule.countries:
        return False
    wanted = rule.locations.get(viewer.country)
    if wanted and not wanted & viewer.locations:
        return False
    if rule.languages and viewer.language not in rule.languages:
        return False
    if rule.topics and not rule.topics & viewer.topics:
        return False
    if rule.exclude_topics & viewer.topics:
        return False
    if rule.audiences and not rule.audiences & viewer.channels:
        return False
    if rule.exclude_audiences & viewer.channels:
        return False
    if rule.devices and viewer.device not in rule.devices:
        return False
    if rule.politics:
        topics = shown_in.topics if shown_in is not None else frozenset()
        if rule.politics == 'only' and POLITICS_TOPIC not in topics:
            return False
        if rule.politics == 'exclude' and (not topics or POLITICS_TOPIC in topics):
            return False
    return True


# ===================================== Нормализация =====================================

def normalize_language(value, known):
    """'en-US' → 'en', 'pt_BR' → 'pt-br' (если такой код есть), иначе None"""
    if not isinstance(value, str) or not value.strip():
        return None
    code = value.strip().lower().replace('_', '-')
    if code in known:
        return code
    base = code.split('-')[0]
    return base if base in known else None


def normalize_device(value):
    return DEVICE_ALIASES.get(value.strip().lower()) if isinstance(value, str) else None


def place_name(name):
    """Название места для сравнения: регистр, апострофы (ʻ ’ ' …) и лишние пробелы не важны"""
    return ' '.join(_APOSTROPHES.sub('', name.lower()).split())


def _without_region_word(name):
    return ' '.join(_REGION_WORDS.sub('', place_name(name)).split())


class LocationIndex:
    """
    Справочник локаций в памяти: название → id (по стране) и «область» каждой локации.
    Сначала точное совпадение названия, потом — без слов «Region / viloyati / область»
    (у городов область записана в region_*, у DB-IP регион может называться иначе)
    """

    def __init__(self, locations):
        # locations — [(id, country, [названия], [названия области])]
        self.country = {}
        self.exact, self.loose = {}, {}
        for location_id, country, names, _ in locations:
            self.country[location_id] = country
            for name in filter(None, names):
                self.exact.setdefault(country, {}).setdefault(place_name(name), set()).add(location_id)
                self.loose.setdefault(country, {}).setdefault(_without_region_word(name), set()).add(location_id)
        self.parents = {}
        for location_id, country, _, regions in locations:
            found = set()
            for region in filter(None, regions):
                found |= self.find(country, region)
            found.discard(location_id)
            self.parents[location_id] = frozenset(found)

    def find(self, country, name):
        exact = self.exact.get(country, {}).get(place_name(name))
        if exact:
            return set(exact)
        return set(self.loose.get(country, {}).get(_without_region_word(name), ()))

    def resolve(self, country, cities, regions):
        """Город и регион из гео-базы → id локаций: город + его область + область по региону"""
        found = set()
        for name in (*cities, *regions):
            found |= self.find(country, name)
        for location_id in list(found):
            found |= self.parents.get(location_id, frozenset())
        return frozenset(found)
