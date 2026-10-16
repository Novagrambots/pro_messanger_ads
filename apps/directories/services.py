"""
Справочники для таргетинга с кэшем на час. Кэш — общий (Redis, если задан CACHE_REDIS_URL),
поэтому правка в админке сбрасывает его сразу для всех воркеров: меняем номер версии,
и все старые ключи становятся ненужными (сами истекут).
"""
import hashlib

from django.core.cache import cache
from django.db.models import Q

from apps.directories.models import Country, Language, Location, Topic

CACHE_TTL = 60 * 60
VERSION_KEY = 'directories:version'
NAME_FIELDS = ('name_en', 'name_ru', 'name_uz')
LOCATIONS_LIMIT = 50


def reset_cache():
    """Вызывается сигналом при любой правке справочника"""
    try:
        cache.incr(VERSION_KEY)
    except ValueError:  # ключа ещё нет
        cache.set(VERSION_KEY, 2, None)


def _cached(name, build):
    version = cache.get_or_set(VERSION_KEY, 1, None)
    key = f'directories:v{version}:{name}'
    data = cache.get(key)
    if data is None:
        data = build()
        cache.set(key, data, CACHE_TTL)
    return data


def languages():
    return _cached('languages', lambda: list(
        Language.objects.filter(is_active=True).values('code', *NAME_FIELDS)))


def topics():
    return _cached('topics', lambda: list(
        Topic.objects.filter(is_active=True).values('id', *NAME_FIELDS)))


def countries():
    return _cached('countries', lambda: list(
        Country.objects.filter(is_active=True).values('code', *NAME_FIELDS)))


def locations(country='', query=''):
    """Активные локации: ?country=UZ и поиск по названию на любом из трёх языков (до 50 штук)"""
    country, query = country.strip().upper(), query.strip()
    key = 'locations:' + hashlib.md5(f'{country}|{query.lower()}'.encode()).hexdigest()

    def build():
        queryset = Location.objects.filter(is_active=True, country__is_active=True)
        if country:
            queryset = queryset.filter(country_id=country)
        if query:
            match = Q()
            for field in NAME_FIELDS:
                match |= Q(**{f'{field}__icontains': query})
            queryset = queryset.filter(match)
        rows = queryset.values('id', 'country_id', *NAME_FIELDS, 'region_en', 'region_ru', 'region_uz')
        # В ответе — код страны под ключом country
        return [{'id': row.pop('id'), 'country': row.pop('country_id'), **row} for row in rows[:LOCATIONS_LIMIT]]

    return _cached(key, build)


# Наборы активных значений — для проверки таргетинга
def language_codes():
    return {item['code'] for item in languages()}


def country_codes():
    return {item['code'] for item in countries()}


def topic_ids():
    return {item['id'] for item in topics()}


def location_countries():
    """{id локации: код страны} — только активные"""
    return _cached('location_countries', lambda: dict(
        Location.objects.filter(is_active=True, country__is_active=True).values_list('id', 'country_id')))
