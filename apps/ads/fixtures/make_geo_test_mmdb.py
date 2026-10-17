"""
ТОЛЬКО ДЛЯ РАЗРАБОТКИ: генерирует тестовую гео-базу geo-test.mmdb для apps/ads/test_serving.py.
Формат и структура записей — как у DB-IP Lite City (страна, город, регионы).

Библиотека mmdb-writer в requirements НЕ входит — ставьте её во временное окружение:
    python -m venv /tmp/mmdbw && /tmp/mmdbw/bin/pip install mmdb-writer
    /tmp/mmdbw/bin/python apps/ads/fixtures/make_geo_test_mmdb.py
"""
from pathlib import Path

from mmdb_writer import MMDBWriter
from netaddr import IPSet

# Сеть → (страна, город, регион). Адреса — из диапазонов для документации и тестов (RFC 5737, 6598)
NETWORKS = {
    '203.0.113.0/24': ('UZ', 'Angren', 'Tashkent Region'),     # город из справочника + его область
    '198.51.100.0/24': ('UZ', 'Tashkent', 'Toshkent Shahri'),  # город, регион не совпадает с областью
    '100.64.0.0/24': ('UZ', 'Gulistonobod', 'Samarqand Region'),  # города нет в справочнике, регион есть
    '192.0.2.0/24': ('KZ', 'Almaty', 'Almaty'),                # другая страна
    '100.64.1.0/24': ('RU', None, None),                       # только страна
}


def record(country, city, region):
    data = {'country': {'iso_code': country, 'names': {'en': country}}}
    if city:
        data['city'] = {'names': {'en': city}}
    if region:
        data['subdivisions'] = [{'names': {'en': region}}]
    return data


def main():
    writer = MMDBWriter(ip_version=6, ipv4_compatible=True, database_type='DBIP-City-Lite',
                        languages=['en'], description={'en': 'new_dvs test geo database'})
    for network, values in NETWORKS.items():
        writer.insert_network(IPSet([network]), record(*values))
    path = Path(__file__).with_name('geo-test.mmdb')
    writer.to_db_file(str(path))
    print(f'{path}: {path.stat().st_size} байт')


if __name__ == '__main__':
    main()
