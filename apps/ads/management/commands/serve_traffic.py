"""
Мониторинг трафика выдачи вручную (то же, что делают задачи monitor_serve_traffic и report_serve_traffic):

    python manage.py serve_traffic --test                 # пример алерта в Telegram — проверить бота и чат
    python manage.py serve_traffic --check [--hour 2026112213] [--dry-run]
    python manage.py serve_traffic --report [--day 2026-11-21] [--dry-run]

--dry-run — сообщение только печатается: без Telegram и без отметок «уже сообщили» в Redis
"""
from datetime import date, datetime

from django.core.management.base import BaseCommand, CommandError

from apps.ads import monitoring
from apps.ads.serving import store as S


class Command(BaseCommand):
    help = 'Мониторинг трафика выдачи: проверка падений за час, отчёт за сутки, тестовое сообщение в Telegram'

    def add_arguments(self, parser):
        action = parser.add_mutually_exclusive_group(required=True)
        action.add_argument('--test', action='store_true', help='Отправить пример алерта в Telegram')
        action.add_argument('--check', action='store_true', help='Проверить час (по умолчанию — прошлый)')
        action.add_argument('--report', action='store_true', help='Отчёт за сутки (по умолчанию — вчера)')
        parser.add_argument('--hour', help='Час для --check: YYYYMMDDHH по Ташкенту')
        parser.add_argument('--day', help='Сутки для --report: YYYY-MM-DD')
        parser.add_argument('--dry-run', action='store_true', help='Только напечатать, ничего не отправлять')

    def handle(self, *args, test=False, check=False, report=False, hour=None, day=None, dry_run=False, **options):
        if test:
            text = monitoring.test_message()
            self.stdout.write(text)
            if not dry_run:
                self._sent(monitoring.send_telegram(text))
        elif check:
            self._check(hour, dry_run)
        else:
            try:
                parsed = date.fromisoformat(day) if day else None
            except ValueError:
                raise CommandError('--day — в формате YYYY-MM-DD')
            self.stdout.write(monitoring.daily_report(parsed, send=not dry_run))

    def _check(self, hour, dry_run):
        if hour:
            try:
                moment = datetime.strptime(hour, '%Y%m%d%H').replace(tzinfo=S.tz())
            except ValueError:
                raise CommandError('--hour — в формате YYYYMMDDHH (по Ташкенту)')
        else:
            moment = monitoring.last_hour()
        saved = monitoring.save_hours(moment)
        result = monitoring.check_drops(moment, send=not dry_run)
        self.stdout.write(f'{monitoring.period(moment)}: строк из Redis — {saved}, '
                          f'упали — {len(result.dropped)}, вернулись — {len(result.recovered)}')
        if dry_run:
            if result.dropped:
                self.stdout.write(monitoring.drop_message(result))
            if result.recovered:
                self.stdout.write(monitoring.recovered_message(result))

    def _sent(self, ok):
        if ok:
            self.stdout.write(self.style.SUCCESS('Telegram: yuborildi'))
        else:
            raise CommandError("Telegram: yuborilmadi (sozlamalar yoki tarmoqni tekshiring, sabab — logda)")
