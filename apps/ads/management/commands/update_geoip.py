from django.core.management.base import BaseCommand

from apps.ads.serving.geo import update_database


class Command(BaseCommand):
    help = ('Скачать свежую гео-базу DB-IP Lite City (страна и город зрителя по IP) в settings.GEOIP_PATH. '
            'Дальше её раз в месяц обновляет beat. IP Geolocation by DB-IP (CC BY 4.0)')

    def handle(self, *args, **options):
        month = update_database()
        self.stdout.write(self.style.SUCCESS(f'Гео-база обновлена: DB-IP Lite City {month}'))
