"""
Мониторинг трафика выдачи (DEV-77): счётчики на горячем пути serve (apps/ads/serving/monitor.py),
перенос часов в Postgres, алерт о резком падении трафика клиента и «tiklandi», суточный отчёт,
отправка в Telegram (сеть замокана) и команда serve_traffic. Redis — ADS_REDIS_TEST_URL, очищается
перед каждым тестом.
"""
import io
import json
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import redis
import requests
from django.conf import settings
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings

from apps.ads import monitoring
from apps.ads.models import ServeTrafficHour, client_label
from apps.ads.serving import monitor, snapshot
from apps.ads.serving import store as S
from apps.ads.test_serving import API_KEY, QUERY, REDIS_DOWN, ServingBase
from apps.ads.tests import setUpModule, tearDownModule  # noqa: F401
from core.celery import app as celery_app

TZ = ZoneInfo('Asia/Tashkent')
HOUR = datetime(2026, 11, 22, 13, tzinfo=TZ)
IOS = ('ios', '-', 'Novagram/89')
ANDROID = ('android', '1.4.0', 'okhttp/3.14.9')
NEW_BUILD = ('ios', '-', 'Novagram/90')
TELEGRAM = {'MONITOR_TELEGRAM_BOT_TOKEN': '123:secret-token', 'MONITOR_TELEGRAM_CHAT_ID': '-1001'}


class ClientOfTests(SimpleTestCase):
    def test_normalizes_platform_version_and_user_agent(self):
        self.assertEqual(monitor.client_of('iOS', '1.2.3 (45)', 'Novagram/89 CFNetwork/1410 Darwin/22.6'),
                         ('ios', '1.2.3', 'Novagram/89'))
        self.assertEqual(monitor.client_of('android', 'v1.4', 'okhttp/3.14.9'), ('android', '1.4', 'okhttp/3.14.9'))
        self.assertEqual(monitor.client_of('iphone', None, None), ('ios', '-', '-'))

    def test_junk_becomes_dash(self):
        # Мусор в полях не плодит тысячи разных клиентов
        self.assertEqual(monitor.client_of('fridge', 'beta', '  '), ('-', '-', '-'))
        self.assertEqual(monitor.client_of(42, ['1'], 7), ('-', '-', '-'))
        self.assertEqual(monitor.client_of(None, '1.2.3.4.5.6', '|evil/1'), ('-', '1.2.3.4', '-'))

    def test_field_roundtrip(self):
        name = monitor.field(monitor.REQUESTS, IOS)
        self.assertEqual(name, 'n|ios|-|Novagram/89')
        self.assertEqual(monitor.parse_field(name), ('n', IOS))
        self.assertEqual(monitor.parse_field('x|a|b|c'), (None, None))
        self.assertEqual(monitor.parse_field('n|a|b'), (None, None))

    def test_label(self):
        self.assertEqual(client_label(IOS), 'iOS · Novagram/89')
        self.assertEqual(client_label(ANDROID), 'Android · okhttp/3.14.9 · 1.4.0')
        self.assertEqual(client_label(('-', '-', '-')), '?')


class ServeCountingTests(ServingBase):
    """Счётчики на горячем пути: запрос и «с объявлением» — в хеш текущего часа"""

    def serve_as(self, query, platform='android', app_version='1.4.0', user_agent='okhttp/3.14.9'):
        body = {'placement': 'SEARCH', 'user_id': 1, 'context': {'query': query},
                'platform': platform, 'app_version': app_version}
        response = self.client.post('/api/ads/serve/', json.dumps(body), content_type='application/json',
                                    HTTP_X_API_KEY=API_KEY, HTTP_USER_AGENT=user_agent)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['ads']

    def test_counts_requests_and_filled_by_client(self):
        self.active_ad()
        self.reload()
        self.assertTrue(self.serve_as(QUERY))
        self.assertFalse(self.serve_as('ничего такого нет'))
        self.assertFalse(self.serve_as('ничего', platform='ios', app_version=None,
                                       user_agent='Novagram/89 CFNetwork/1410'))
        hour = monitor.read_hour(monitor.hour_of())
        self.assertEqual(hour, {ANDROID: [2, 1], IOS: [1, 0]})
        self.assertGreater(self.r.ttl(monitor.hour_key(monitor.hour_of())), 2 * 86400)

    def test_still_no_sql_on_hot_path(self):
        self.active_ad()
        self.reload()
        with self.assertNumQueries(0):
            self.assertTrue(self.serve_as(QUERY))

    def test_rejected_request_is_not_counted(self):
        response = self.client.post('/api/ads/serve/', json.dumps({'placement': 'NOPE', 'user_id': 1}),
                                    content_type='application/json', HTTP_X_API_KEY=API_KEY)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(monitor.read_hour(monitor.hour_of()), {})

    @override_settings(ADS_REDIS_URL=REDIS_DOWN)
    def test_redis_error_does_not_break_caller(self):
        monitor._last_error_log = 0.0
        with self.assertLogs('apps.ads.serving.monitor', 'WARNING'):
            monitor.count_serve(IOS, filled=True)  # не бросает


class MonitoringBase(TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            S.store().redis.ping()
        except redis.RedisError:
            raise unittest.SkipTest(f'Нет Redis для тестов выдачи ({settings.ADS_REDIS_URL})')
        super().setUpClass()

    def setUp(self):
        self.r = S.store().redis
        self.r.flushdb()
        snapshot._current = None
        sender = patch('apps.ads.monitoring.send_telegram', return_value=True)
        self.send = sender.start()
        self.addCleanup(sender.stop)

    @staticmethod
    def put(hour, client, requests_count, filled=0):
        platform, version, product = client
        ServeTrafficHour.objects.update_or_create(
            hour=hour, platform=platform, app_version=version, user_agent=product,
            defaults={'requests': requests_count, 'filled': filled})

    def history(self, client, *daily, hour=HOUR):
        """Тот же час 1, 2, 3… суток назад"""
        for back, count in enumerate(daily, start=1):
            if count is not None:
                self.put(hour - timedelta(days=back), client, count, count // 2)


class SaveHoursTests(MonitoringBase):
    def write(self, hour, client, requests_count, filled):
        key = monitor.hour_key(monitor.hour_of(hour))
        self.r.hset(key, mapping={monitor.field(monitor.REQUESTS, client): requests_count,
                                  monitor.field(monitor.FILLED, client): filled})

    def test_redis_hours_to_postgres_idempotent(self):
        self.write(HOUR, IOS, 5000, 3000)
        self.write(HOUR - timedelta(hours=2), ANDROID, 800, 900)  # fill больше запросов — обрезаем
        self.r.hset(monitor.hour_key(monitor.hour_of(HOUR)), 'garbage', 1)
        self.assertEqual(monitoring.save_hours(HOUR), 2)
        self.write(HOUR, IOS, 5100, 3050)  # час дописался — строка обновилась, а не задвоилась
        monitoring.save_hours(HOUR)
        rows = {(row.hour, row.client): (row.requests, row.filled) for row in ServeTrafficHour.objects.all()}
        self.assertEqual(rows, {(HOUR, IOS): (5100, 3050), (HOUR - timedelta(hours=2), ANDROID): (800, 800)})

    def test_hours_outside_window_untouched(self):
        self.write(HOUR - timedelta(hours=monitoring.SAVE_HOURS), IOS, 10, 1)
        self.assertEqual(monitoring.save_hours(HOUR), 0)


@override_settings(MONITOR_BASELINE_DAYS=3, MONITOR_MIN_HOURLY=200, MONITOR_DROP_RATIO=0.3, MONITOR_LABEL='prod')
class CheckDropsTests(MonitoringBase):
    def test_sharp_drop_alerts_once_then_recovers(self):
        self.history(IOS, 5000, 5200, 5100)
        self.history(ANDROID, 3000, 3000, 3000)
        self.put(HOUR, IOS, 400, 100)       # −92% — падение
        self.put(HOUR, ANDROID, 2500, 1200)  # −17% — норма

        check = monitoring.check_drops(HOUR)
        self.assertEqual(check.dropped, [(IOS, 400, 5100, 100)])
        self.assertEqual(check.recovered, [])
        self.assertEqual(check.platforms, {'ios': (400, 5100)})
        text = self.send.call_args.args[0]
        self.assertIn('[prod] Reklama trafigi keskin tushdi — 22.11 13:00–14:00', text)
        self.assertIn('• iOS · Novagram/89: 400 ta serve (odatda ~5 100, -92%), fill 25%', text)
        self.assertIn('iOS jami: 400 (odatda ~5 100)', text)
        self.assertNotIn('Android', text)
        self.assertTrue(self.r.exists(monitoring.alert_key(IOS)))

        # Через час всё ещё мало — второй раз не пишем (напоминание — через сутки, TTL отметки)
        self.send.reset_mock()
        later = HOUR + timedelta(hours=1)
        self.history(IOS, 5000, 5000, 5000, hour=later)
        self.put(later, IOS, 300)
        self.assertEqual(monitoring.check_drops(later).dropped, [])
        self.send.assert_not_called()
        self.assertLessEqual(self.r.ttl(monitoring.alert_key(IOS)), monitoring.ALERT_TTL)

        # Вернулся — «tiklandi», отметка снята
        again = HOUR + timedelta(hours=2)
        self.history(IOS, 5000, 5000, 5000, hour=again)
        self.put(again, IOS, 4800, 2400)
        check = monitoring.check_drops(again)
        self.assertEqual(check.recovered, [(IOS, 4800, 5000, 2400)])
        self.assertIn('✅ [prod] Reklama trafigi tiklandi', self.send.call_args.args[0])
        self.assertFalse(self.r.exists(monitoring.alert_key(IOS)))

    def test_zero_traffic_alerts(self):
        # Как у iOS 89 в ноябре: строк за час нет совсем
        self.history(IOS, 5000, 5000, 5000)
        check = monitoring.check_drops(HOUR)
        self.assertEqual(check.dropped, [(IOS, 0, 5000, 0)])
        self.assertIn('0 ta serve (odatda ~5 000, -100%), fill —', self.send.call_args.args[0])

    def test_small_and_new_clients_ignored(self):
        self.history(('ios', '-', 'Novagram/86'), 150, 150, 150)  # обычный трафик < 200 в час
        self.history(NEW_BUILD, 3000)                              # есть только вчера: медиана [3000, 0, 0] = 0
        check = monitoring.check_drops(HOUR)
        self.assertEqual((check.dropped, check.recovered), ([], []))
        self.send.assert_not_called()

    def test_two_of_three_days_are_enough(self):
        self.history(NEW_BUILD, 3000, 2800)  # медиана [3000, 2800, 0] = 2800
        self.assertEqual(monitoring.check_drops(HOUR).dropped, [(NEW_BUILD, 0, 2800, 0)])

    def test_failed_send_retries_next_hour(self):
        self.history(IOS, 5000, 5000, 5000)
        self.send.return_value = False
        self.assertEqual(len(monitoring.check_drops(HOUR).dropped), 1)
        self.assertFalse(self.r.exists(monitoring.alert_key(IOS)))  # отметки нет — в следующий час снова попробуем

    def test_dry_run_sends_nothing(self):
        self.history(IOS, 5000, 5000, 5000)
        self.assertEqual(len(monitoring.check_drops(HOUR, send=False).dropped), 1)
        self.send.assert_not_called()
        self.assertFalse(self.r.exists(monitoring.alert_key(IOS)))

    def test_run_hourly_saves_last_hour_then_checks(self):
        self.history(IOS, 5000, 5000, 5000)
        key = monitor.hour_key(monitor.hour_of(HOUR))
        self.r.hset(key, mapping={monitor.field(monitor.REQUESTS, IOS): 4900,
                                  monitor.field(monitor.FILLED, IOS): 2000})
        check = monitoring.run_hourly(now=HOUR + timedelta(minutes=65))  # 14:05 → проверяем 13:00–14:00
        self.assertEqual(check.hour, HOUR)
        self.assertEqual(check.dropped, [])
        self.assertEqual(ServeTrafficHour.objects.get(hour=HOUR).requests, 4900)

    @override_settings(ADS_REDIS_URL=REDIS_DOWN)
    def test_run_hourly_reports_redis_outage(self):
        with self.assertLogs('apps.ads.monitoring', 'WARNING'):
            self.assertIsNone(monitoring.run_hourly(now=HOUR + timedelta(minutes=65)))
        self.assertIn('Redis выдачи (redis_ads) javob bermayapti', self.send.call_args.args[0])


@override_settings(MONITOR_MIN_HOURLY=200, MONITOR_LABEL='prod')
class DailyReportTests(MonitoringBase):
    DAY = date(2026, 11, 21)

    def test_report_totals_clients_changes_and_gone(self):
        start = datetime(2026, 11, 21, tzinfo=TZ)
        self.put(start + timedelta(hours=9), IOS, 60000, 39000)
        self.put(start + timedelta(hours=10), IOS, 40000, 26000)
        self.put(start + timedelta(hours=9), ANDROID, 20000, 8000)
        self.put(start + timedelta(hours=9), NEW_BUILD, 300, 150)
        self.put(start - timedelta(hours=5), IOS, 80000, 50000)            # позавчера
        self.put(start - timedelta(hours=5), ANDROID, 25000, 10000)
        self.put(start - timedelta(hours=5), ('ios', '-', 'Novagram/88'), 5000, 2000)  # вчера пропала
        self.put(start + timedelta(days=1), IOS, 99999)                   # сегодня — не в отчёте

        text = monitoring.daily_report(self.DAY)
        self.assertEqual(text, self.send.call_args.args[0])
        lines = text.split('\n')
        self.assertEqual(lines[0], '📊 [prod] Reklama trafigi — 21.11.2026')
        self.assertEqual(lines[1], 'Jami: 120 300 serve, fill 61% (oldingi kunga +9%)')
        self.assertEqual(lines[2], '• iOS · Novagram/89: 100 000, fill 65%, +25%')
        self.assertEqual(lines[3], '• Android · okhttp/3.14.9 · 1.4.0: 20 000, fill 40%, -20%')
        self.assertEqual(lines[4], '• iOS · Novagram/90: 300, fill 50%, yangi')
        self.assertEqual(lines[5], "Yo'qolgan (oldingi kuni bor edi): iOS · Novagram/88")

    def test_empty_day(self):
        self.assertIn("Ma'lumot yo'q", monitoring.daily_report(self.DAY, send=False))
        self.send.assert_not_called()

    def test_top_and_rest(self):
        start = datetime(2026, 11, 21, tzinfo=TZ)
        for build in range(monitoring.REPORT_TOP + 2):
            self.put(start, ('ios', '-', f'Novagram/{build}'), 1000 + build)
        lines = monitoring.daily_report(self.DAY, send=False).split('\n')
        self.assertEqual(lines[-1], '• boshqalar (2 ta): 2 001')


class SendTelegramTests(SimpleTestCase):
    @override_settings(MONITOR_TELEGRAM_BOT_TOKEN='', MONITOR_TELEGRAM_CHAT_ID='')
    def test_not_configured_only_logs(self):
        with patch('apps.ads.monitoring.requests.post') as post, self.assertLogs('apps.ads.monitoring', 'WARNING'):
            self.assertFalse(monitoring.send_telegram('salom'))
        post.assert_not_called()

    @override_settings(**TELEGRAM)
    def test_sends_message(self):
        with patch('apps.ads.monitoring.requests.post', return_value=Mock(status_code=200)) as post:
            self.assertTrue(monitoring.send_telegram('x' * 5000))
        url = post.call_args.args[0]
        self.assertEqual(url, 'https://api.telegram.org/bot123:secret-token/sendMessage')
        payload = post.call_args.kwargs['json']
        self.assertEqual(payload['chat_id'], '-1001')
        self.assertEqual(len(payload['text']), monitoring.TELEGRAM_LIMIT)

    @override_settings(**TELEGRAM)
    def test_errors_never_log_token(self):
        failures = [Mock(status_code=403, text='Forbidden: bot was kicked'),
                    requests.ConnectionError('https://api.telegram.org/bot123:secret-token/sendMessage')]
        for failure in failures:
            kwargs = {'side_effect': failure} if isinstance(failure, Exception) else {'return_value': failure}
            with patch('apps.ads.monitoring.requests.post', **kwargs), \
                    self.assertLogs('apps.ads.monitoring', 'WARNING') as logs:
                self.assertFalse(monitoring.send_telegram('salom'))
            self.assertNotIn('secret-token', '\n'.join(logs.output))


class CommandAndScheduleTests(MonitoringBase):
    def run_command(self, *args):
        out = io.StringIO()
        call_command('serve_traffic', *args, stdout=out)
        return out.getvalue()

    def test_test_message(self):
        output = self.run_command('--test')
        self.assertIn('SINOV xabari', output)
        self.assertIn('Telegram: yuborildi', output)
        self.assertIn('SINOV xabari', self.send.call_args.args[0])

    @override_settings(MONITOR_BASELINE_DAYS=3, MONITOR_MIN_HOURLY=200, MONITOR_DROP_RATIO=0.3)
    def test_check_dry_run_for_given_hour(self):
        self.history(IOS, 5000, 5000, 5000)
        output = self.run_command('--check', '--hour', '2026112213', '--dry-run')
        self.assertIn('22.11 13:00–14:00: строк из Redis — 0, упали — 1, вернулись — 0', output)
        self.assertIn('Reklama trafigi keskin tushdi', output)
        self.send.assert_not_called()

    def test_report_dry_run(self):
        self.assertIn('21.11.2026', self.run_command('--report', '--day', '2026-11-21', '--dry-run'))
        self.send.assert_not_called()

    def test_beat_schedule(self):
        from apps.ads import tasks
        scheduled = {entry['task'] for entry in celery_app.conf.beat_schedule.values()}
        for task in (tasks.monitor_serve_traffic, tasks.report_serve_traffic):
            self.assertIn(task.name, scheduled)
        with patch('apps.ads.monitoring.run_hourly') as hourly, patch('apps.ads.monitoring.daily_report') as daily:
            tasks.monitor_serve_traffic()
            tasks.report_serve_traffic()
        hourly.assert_called_once_with()
        daily.assert_called_once_with()
