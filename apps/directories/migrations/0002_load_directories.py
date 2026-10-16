"""
Начальные данные справочников из apps/directories/data/*.json:
71 язык и 41 тема как у Telegram, 249 стран ISO 3166-1 + XK, 36 локаций Узбекистана (из AdGram).
Дальше справочники правит суперадмин в админке — правки файлов сюда уже не попадут.
"""
import json
from pathlib import Path

from django.db import migrations

DATA_DIR = Path(__file__).resolve().parent.parent / 'data'


def read(name):
    return json.loads((DATA_DIR / name).read_text(encoding='utf-8'))


def load(apps, schema_editor):
    Language = apps.get_model('directories', 'Language')
    Topic = apps.get_model('directories', 'Topic')
    Country = apps.get_model('directories', 'Country')
    Location = apps.get_model('directories', 'Location')

    Language.objects.bulk_create([Language(**item) for item in read('languages.json')], ignore_conflicts=True)
    Topic.objects.bulk_create([Topic(**item) for item in read('topics.json')], ignore_conflicts=True)
    Country.objects.bulk_create([Country(**item) for item in read('countries.json')], ignore_conflicts=True)
    if not Location.objects.exists():
        Location.objects.bulk_create([
            Location(country_id=item.pop('country'), **item) for item in read('locations.json')
        ])


class Migration(migrations.Migration):

    dependencies = [
        ('directories', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(load, migrations.RunPython.noop),
    ]
