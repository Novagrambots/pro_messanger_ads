"""
Тесты выдачи v2 (apps/ads/serving) на настоящем Redis: выбор, токены, события, сброс в Postgres,
заморозка перед возвратом, ноль SQL на горячем пути, гонка параллельных показов в потоках,
порядок блокировок «объявление → баланс», очереди Celery.
Redis — ADS_REDIS_TEST_URL (по умолчанию redis://localhost:6379/15), очищается перед каждым тестом.
"""
import json
import threading
import time
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import redis
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.ads.models import Ad, AdDailyStat, AdStatus, FlushBatch, Placement
from apps.ads.serving import events, flush as flushing, selection, snapshot, store as S, tokens
from apps.ads.serving import views as serving_views
from apps.ads.tests import KUNUZ_ID, client_for, create_ad, make_local_user, setUpModule, tearDownModule  # noqa: F401
from apps.billing import services as billing_services
from apps.billing.models import Balance, MoneyOperation as Op
from apps.directories.models import TelegramPeer
from apps.moderation.test_ads import make_moderator
from apps.notifications.models import Notification

User = get_user_model()

API_KEY = 'test-api-key'
SECRETS = {'VIEWER_HASH_SECRET': 'viewer-secret', 'ADS_TOKEN_SECRET': 'token-secret'}
# Лимиты запросов в тестах выключены (кроме теста лимитов)
SERVING_SETTINGS = {'AD_SERVER_API_KEY': API_KEY, **SECRETS,
                    'ADS_RATE_VIEWER_SERVE': 0, 'ADS_RATE_VIEWER_EVENTS': 0, 'ADS_RATE_IP': 0}
REDIS_DOWN = 'redis://127.0.0.1:1/0'  # порт, где никого нет — соединение отклоняется сразу
QUERY = 'новости uz'
SEEN = {'duration_ms': 1500, 'visible_ratio': 0.8}  # просмотр, который засчитывается


@override_settings(**SERVING_SETTINGS)
class ServingBase(TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            S.store().redis.ping()
        except redis.RedisError:
            raise unittest.SkipTest(f'Нет Redis для тестов выдачи ({settings.ADS_REDIS_URL}) — '
                                    f'поднимите его или задайте ADS_REDIS_TEST_URL')
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        # Один раз на класс: хеширование паролей — самая медленная часть теста
        cls.owner = make_local_user('owner')
        cls.moderator = make_moderator()

    def setUp(self):
        self.r = S.store().redis
        self.r.flushdb()
        snapshot._current = None  # снимок — глобальный на процесс, у каждого теста свой
        push = patch('apps.notifications.services._enqueue_push')  # push в тестах не шлём
        push.start()
        self.addCleanup(push.stop)
        self.owner_client = client_for(self.owner)
        self.mod_client = client_for(self.moderator)

    # ------------------------------------ хелперы ------------------------------------

    def active_ad(self, queries=(QUERY,), cpm=1000, budget='10000', **fields):
        """Объявление в поиске: создано владельцем (деньги списаны) и одобрено модератором"""
        response = create_ad(self.owner_client, Placement.SEARCH, cpm=cpm, budget=budget,
                             targeting={'queries': list(queries)})
        self.assertEqual(response.status_code, 201, response.data)
        ad_id = response.data['id']
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.mod_client.post(f'/api/moderation/ads/{ad_id}/approve/').status_code, 200)
        if fields:
            Ad.objects.filter(pk=ad_id).update(**fields)
        return Ad.objects.get(pk=ad_id)

    def reload(self):
        return snapshot.reload()

    def serve(self, user_id=1, query=QUERY, placement=Placement.SEARCH, **headers):
        body = {'placement': placement, 'user_id': user_id, 'context': {'query': query}, 'lang': 'uz'}
        return self.client.post('/api/ads/serve/', json.dumps(body), content_type='application/json',
                                HTTP_X_API_KEY=API_KEY, **headers)

    def serve_ad(self, user_id=1, query=QUERY):
        response = self.serve(user_id, query)
        self.assertEqual(response.status_code, 200, response.content)
        ads = response.json()['ads']
        return ads[0] if ads else None

    def send(self, user_id, *items, **headers):
        body = {'user_id': user_id, 'events': list(items)}
        return self.client.post('/api/ads/events/', json.dumps(body), content_type='application/json',
                                HTTP_X_API_KEY=API_KEY, **headers)

    def sent(self, user_id, *items):
        response = self.send(user_id, *items)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        return data['accepted'], data['rejected']

    @staticmethod
    def view(random_id, **extra):
        return {'random_id': random_id, 'type': 'view', **SEEN, **extra}

    @staticmethod
    def click(random_id):
        return {'random_id': random_id, 'type': 'click'}

    @staticmethod
    def token(ad, user_id=1):
        """random_id, как его выдал бы serve этому зрителю"""
        return tokens.issue(str(ad.pk), tokens.viewer_key(user_id), Placement.SEARCH,
                            tokens.context_hash(Placement.SEARCH, QUERY))

    def flush(self):
        with self.captureOnCommitCallbacks(execute=True):
            return flushing.flush()

    def budget_left(self, ad):
        value = self.r.get(S.budget_key(ad.pk))
        return int(value) if value is not None else None

    def stat(self, ad):
        return AdDailyStat.objects.filter(ad=ad, date=timezone.localdate()).first()

    def fresh(self, ad):
        return Ad.objects.get(pk=ad.pk)

    def assert_redis_clean(self):
        """После сброса в Redis не осталось несброшенных дельт и пачек"""
        self.assertEqual(self.r.keys(f'{S.PREFIX}delta:*') + self.r.keys(f'{S.PREFIX}flush:*')
                         + self.r.keys(f'{S.PREFIX}batch:*'), [])
        self.assertEqual(self.r.zcard(S.BATCHES), 0)


class SelectionTests(ServingBase):
    """Выбор: аукцион, частота, дневной лимит, остаток, расписание, нормализация фраз"""

    def test_highest_cpm_wins_and_pays_own_bid(self):
        self.active_ad(cpm=1000)
        rich = self.active_ad(cpm=3000)
        self.reload()
        ad = self.serve_ad()
        self.assertEqual(tokens.decode(ad['random_id']).ad_id, str(rich.pk))
        self.assertEqual(self.sent(1, self.view(ad['random_id'])), (1, 0))
        self.assertEqual(self.budget_left(rich), 10_000_000 - 3000)  # своя ставка: 3 сума за показ

    def test_equal_cpm_is_random(self):
        first, second = self.active_ad(), self.active_ad()
        self.reload()
        picked = {tokens.decode(self.serve_ad(user_id)['random_id']).ad_id for user_id in range(1, 60)}
        self.assertEqual(picked, {str(first.pk), str(second.pk)})

    def test_views_per_user_per_day(self):
        ad = self.active_ad()  # по умолчанию — 1 показ одному человеку в сутки
        snap = self.reload()
        first = self.serve_ad(1)
        self.assertEqual(self.sent(1, self.view(first['random_id'])), (1, 0))
        self.assertIsNone(self.serve_ad(1))     # этому зрителю сегодня хватит
        self.assertIsNotNone(self.serve_ad(2))  # другому показываем
        # Сутки по Ташкенту: завтра снова можно
        tomorrow = timezone.now() + timedelta(days=1)
        self.assertIsNotNone(selection.pick(snap, Placement.SEARCH, QUERY, tokens.viewer_key(1), now=tomorrow))

        Ad.objects.filter(pk=ad.pk).update(views_per_user_per_day=0)  # без ограничений
        self.reload()
        self.assertIsNotNone(self.serve_ad(1))

    def test_daily_limit(self):
        # 1 сум за показ, дневной лимит 2 сума
        ad = self.active_ad(cpm=1000, daily_limit=2, views_per_user_per_day=0)
        self.reload()
        for user_id in (1, 2):
            self.assertEqual(self.sent(user_id, self.view(self.serve_ad(user_id)['random_id'])), (1, 0))
        self.assertIsNone(self.serve_ad(3))  # лимит исчерпан — не выдаём
        # Показ по уже выданному токену после конца лимита — бесплатный
        self.assertEqual(self.sent(4, self.view(self.token(ad, 4))), (1, 0))
        self.flush()
        stat = self.stat(ad)
        self.assertEqual((stat.views, stat.free_views, stat.spent), (2, 1, Decimal('2')))
        self.assertEqual(self.fresh(ad).spent, Decimal('2'))
        self.assertEqual(self.fresh(ad).status, AdStatus.ACTIVE)  # дневной лимит не останавливает

    def test_remaining_budget_below_price(self):
        ad = self.active_ad(cpm=5_000_000, views_per_user_per_day=0)  # 5000 сумов за показ, бюджета на 2
        self.reload()
        for user_id in (1, 2):
            self.assertEqual(self.sent(user_id, self.view(self.serve_ad(user_id)['random_id'])), (1, 0))
        self.assertEqual(self.budget_left(ad), 0)
        self.assertIsNone(self.serve_ad(3))

    def test_schedule_in_ad_timezone(self):
        tokyo = ZoneInfo('Asia/Tokyo')
        now = datetime(2026, 10, 19, 3, 30, tzinfo=ZoneInfo('UTC'))  # в Токио 12:30
        days = [0] * 7
        days[now.astimezone(tokyo).weekday()] = 1 << 12  # только 12:00–13:00 этого дня недели
        ad = self.active_ad(schedule={'enabled': True, 'days': days, 'timezone': 'Asia/Tokyo'})
        snap = self.reload()
        viewer = tokens.viewer_key(1)

        self.assertEqual(selection.pick(snap, Placement.SEARCH, QUERY, viewer, now=now).id, str(ad.pk))
        self.assertIsNone(selection.pick(snap, Placement.SEARCH, QUERY, viewer, now=now + timedelta(hours=1)))
        self.assertIsNone(selection.pick(snap, Placement.SEARCH, QUERY, viewer, now=now + timedelta(days=1)))
        # В Ташкенте в этот момент 8:30 — значит, считаем именно по поясу объявления
        Ad.objects.filter(pk=ad.pk).update(schedule={'enabled': True, 'days': days, 'timezone': 'Asia/Tashkent'})
        snap = self.reload()
        self.assertIsNone(selection.pick(snap, Placement.SEARCH, QUERY, viewer, now=now))
        # Расписание выключено — круглосуточно
        Ad.objects.filter(pk=ad.pk).update(schedule={'enabled': False, 'days': [0] * 7, 'timezone': 'Asia/Tokyo'})
        snap = self.reload()
        self.assertIsNotNone(selection.pick(snap, Placement.SEARCH, QUERY, viewer, now=now + timedelta(hours=5)))

    def test_query_normalization(self):
        self.assertEqual(snapshot.normalize_query("  O‘zbekiston   YANGILIKLARI \t"), "o'zbekiston yangiliklari")
        self.active_ad(queries=['O‘zbekiston yangiliklari'])
        self.reload()
        variants = ["oʻzbekiston yangiliklari", "O’zbekiston  yangiliklari", "o`zbekiston\tyangiliklari",
                    "o'zbekiston yangiliklari", "OʼZBEKISTON YANGILIKLARI", "  o´zbekiston yangiliklari "]
        for user_id, query in enumerate(variants, start=1):
            self.assertIsNotNone(self.serve_ad(user_id, query), query)
        self.assertIsNone(self.serve_ad(50, "o'zbekiston"))  # только точное совпадение фразы
        self.assertIsNone(self.serve_ad(51, "o'zbekiston yangiliklari bugun"))

    def test_response_has_only_public_fields(self):
        ad = self.active_ad()
        TelegramPeer.objects.filter(username='kunuz').update(avatar='tg_avatars/kunuz_abcd1234.jpg')
        self.reload()
        data = self.serve().json()
        self.assertEqual(data['ttl_seconds'], settings.ADS_CACHE_TTL)
        item = data['ads'][0]
        self.assertEqual(set(item), {'random_id', 'placement', 'text', 'url', 'promoted', 'button_type', 'media',
                                     'other_info', 'label'})
        self.assertEqual(item['promoted'], {
            'username': 'kunuz', 'title': 'kunuz', 'telegram_id': KUNUZ_ID, 'type': 'CHANNEL',
            'avatar_url': 'http://testserver/media/tg_avatars/kunuz_abcd1234.jpg',
        })
        self.assertEqual((item['placement'], item['text'], item['url'], item['button_type'], item['media'],
                          item['label']), (Placement.SEARCH, '', ad.url, 'CHANNEL', None, 'Ad'))
        self.assertEqual(len(item['random_id']), 94)
        self.assertNotIn('cpm', json.dumps(data))

    def test_only_active_ads(self):
        ad = self.active_ad()
        for status in (AdStatus.ON_HOLD, AdStatus.STOPPED, AdStatus.BLOCKED, AdStatus.IN_REVIEW):
            Ad.objects.filter(pk=ad.pk).update(status=status)
            self.reload()
            self.assertIsNone(self.serve_ad(), status)

    def test_other_placements_are_empty_for_now(self):
        self.active_ad()
        self.reload()
        response = self.serve(placement=Placement.BOT)
        self.assertEqual(response.json(), {'ads': [], 'ttl_seconds': settings.ADS_CACHE_TTL})

    def test_bad_requests(self):
        def post(body, key=API_KEY, raw=None):
            return self.client.post('/api/ads/serve/', raw or json.dumps(body), content_type='application/json',
                                    HTTP_X_API_KEY=key)

        good = {'placement': 'SEARCH', 'user_id': 1, 'context': {'query': QUERY}}
        self.assertEqual(post(good, key='').status_code, 403)
        self.assertEqual(post(good, key='wrong').status_code, 403)
        self.assertEqual(post({}, raw='{not json').status_code, 400)
        self.assertEqual(post({}, raw='[1]').status_code, 400)
        for user_id in ('abc', True, -5, 0, 2 ** 60, None, 1.5):
            self.assertEqual(post({**good, 'user_id': user_id}).status_code, 400, user_id)
        self.assertEqual(post({**good, 'user_id': '12345'}).status_code, 200)  # строка из цифр — можно
        self.assertEqual(post({**good, 'placement': 'XYZ'}).status_code, 400)
        self.assertEqual(post({**good, 'context': {}}).status_code, 400)
        self.assertEqual(post({**good, 'context': {'query': '   '}}).status_code, 400)
        self.assertEqual(post({**good, 'context': 'x'}).status_code, 400)
        self.assertEqual(post({**good, 'lang': 5}).status_code, 400)
        self.assertEqual(self.client.get('/api/ads/serve/', HTTP_X_API_KEY=API_KEY).status_code, 405)
        with override_settings(VIEWER_HASH_SECRET=''), self.assertLogs('apps.ads.serving.views', 'ERROR'):
            self.assertEqual(post(good).status_code, 503)  # секреты не заданы — выдача закрыта

    def test_rate_limits(self):
        self.active_ad()
        self.reload()
        with override_settings(ADS_RATE_VIEWER_SERVE=2):
            self.assertEqual([self.serve(1).status_code for _ in range(3)], [200, 200, 429])
            self.assertEqual(self.serve(2).status_code, 200)  # у другого зрителя свой счётчик
        self.r.flushdb()
        with override_settings(ADS_RATE_IP=3):
            ip = {'HTTP_CF_CONNECTING_IP': '203.0.113.7'}
            self.assertEqual([self.serve(user_id, **ip).status_code for user_id in (1, 2, 3, 4)], [200, 200, 200, 429])
            self.assertEqual(self.serve(5, HTTP_CF_CONNECTING_IP='203.0.113.8').status_code, 200)
            self.assertEqual(self.send(6, self.click('x'), **ip).status_code, 429)  # IP-лимит общий с событиями

    def test_redis_down(self):
        ad = self.active_ad()
        self.reload()
        serving_views._last_outage_log = 0
        with override_settings(ADS_REDIS_URL=REDIS_DOWN), self.assertLogs('apps.ads.serving.views', 'ERROR'):
            self.assertEqual(self.serve().json(), {'ads': [], 'ttl_seconds': settings.ADS_EMPTY_TTL})
            self.assertEqual(self.send(1, self.view(self.token(ad))).status_code, 503)  # клиент пришлёт позже


@override_settings(**SECRETS, ADS_TOKEN_TTL=3600)
class TokenTests(SimpleTestCase):
    """random_id и viewer_key — без Redis"""

    AD_ID = '3f0c2b7e-1d4a-4c6b-9a51-0e8f7d6c5b4a'

    def issue(self, user_id=1, now=None):
        return tokens.issue(self.AD_ID, tokens.viewer_key(user_id), Placement.SEARCH,
                            tokens.context_hash(Placement.SEARCH, QUERY), now=now)

    def test_roundtrip(self):
        value = self.issue(now=1_800_000_000)
        token = tokens.decode(value, now=1_800_000_100)
        self.assertEqual((token.ad_id, token.viewer, token.placement, token.issued),
                         (self.AD_ID, tokens.viewer_key(1), Placement.SEARCH, 1_800_000_000))
        self.assertEqual(token.context, tokens.context_hash(Placement.SEARCH, QUERY))
        self.assertEqual(len(value), 94)
        self.assertNotEqual(self.issue(now=1_800_000_000), value)  # nonce — каждый токен уникален

    def test_forgery(self):
        value = self.issue()
        for position in (0, 10, 40, 70, 92):  # последний символ несёт и биты-заполнители
            forged = value[:position] + ('A' if value[position] != 'A' else 'B') + value[position + 1:]
            self.assertIsNone(tokens.decode(forged), position)
        for garbage in ('', 'abc', value[:-2], value + 'AA', '!' * 94, None, 123, 'A' * 500):
            self.assertIsNone(tokens.decode(garbage))
        with override_settings(ADS_TOKEN_SECRET='other-secret'):
            self.assertIsNone(tokens.decode(value))  # подписан другим секретом

    def test_expiry(self):
        value = self.issue(now=1_800_000_000)
        self.assertIsNotNone(tokens.decode(value, now=1_800_000_000 + 3600))
        self.assertIsNone(tokens.decode(value, now=1_800_000_000 + 3601))
        self.assertIsNone(tokens.decode(value, now=1_800_000_000 - 120))  # «из будущего»

    def test_viewer_key(self):
        key = tokens.viewer_key(123456789)
        self.assertRegex(key, r'^[0-9a-f]{32}$')
        self.assertNotIn('123456789', key)
        self.assertEqual(key, tokens.viewer_key(123456789))
        self.assertNotEqual(key, tokens.viewer_key(123456780))
        with override_settings(VIEWER_HASH_SECRET='other'):
            self.assertNotEqual(key, tokens.viewer_key(123456789))

    def test_parse_user_id(self):
        self.assertEqual(tokens.parse_user_id(42), 42)
        self.assertEqual(tokens.parse_user_id('42'), 42)
        for bad in (0, -1, True, '4a', '', None, 4.2, 2 ** 53, '1' * 30, [1]):
            self.assertIsNone(tokens.parse_user_id(bad), bad)


class EventTests(ServingBase):
    """События: пороги просмотра, дубли, частота, бесплатный показ, клик 5 с, чужой токен"""

    def setUp(self):
        super().setUp()
        self.ad = self.active_ad(views_per_user_per_day=0)
        self.reload()

    def test_view_thresholds(self):
        cases = [
            ({'duration_ms': 999, 'visible_ratio': 0.9}, 0),
            ({'duration_ms': 5000, 'visible_ratio': 0.49}, 0),
            ({'duration_ms': 5000, 'visible_ratio': 1.5}, 0),
            ({'duration_ms': True, 'visible_ratio': 0.9}, 0),
            ({'duration_ms': '2000', 'visible_ratio': 0.9}, 0),
            ({'visible_ratio': 0.9}, 0),
            ({'duration_ms': 1000, 'visible_ratio': 0.5}, 1),  # ровно на пороге — засчитываем
        ]
        for user_id, (seen, accepted) in enumerate(cases, start=1):
            event = {'random_id': self.token(self.ad, user_id), 'type': 'view', **seen}
            self.assertEqual(self.sent(user_id, event), (accepted, 1 - accepted), seen)
        self.assertEqual(self.budget_left(self.ad), 10_000_000 - 1000)

    def test_one_view_and_one_click_per_token(self):
        token = self.token(self.ad)
        self.assertEqual(self.sent(1, self.view(token), self.view(token)), (1, 1))
        self.assertEqual(self.sent(1, self.view(token)), (0, 1))
        self.assertEqual(self.sent(1, self.click(token)), (1, 0))
        self.assertEqual(self.sent(1, self.click(token)), (0, 1))
        self.assertEqual(self.budget_left(self.ad), 10_000_000 - 1000)  # списали один раз

    def test_foreign_token_rejected(self):
        token = self.token(self.ad, user_id=1)
        self.assertEqual(self.sent(2, self.view(token), self.click(token)), (0, 2))  # переслали другому
        self.assertEqual(self.sent(1, self.view(token)), (1, 0))

    def test_frequency_rejects_extra_views(self):
        Ad.objects.filter(pk=self.ad.pk).update(views_per_user_per_day=2)
        self.reload()
        results = [self.sent(1, self.view(self.token(self.ad)))[0] for _ in range(3)]
        self.assertEqual(results, [1, 1, 0])
        self.flush()
        self.assertEqual(self.stat(self.ad).views, 2)

    def test_free_view_after_budget_end(self):
        ad = self.active_ad(cpm=5_000_000, views_per_user_per_day=0)  # бюджета на 2 показа
        self.reload()
        results = [self.sent(user_id, self.view(self.token(ad, user_id)))[0] for user_id in (1, 2, 3)]
        self.assertEqual(results, [1, 1, 1])  # третий — бесплатный, но тоже принят
        self.assertEqual(self.budget_left(ad), 0)
        self.flush()
        stat = self.stat(ad)
        self.assertEqual((stat.views, stat.free_views, stat.spent), (2, 1, Decimal('10000')))

    def test_view_of_not_active_ad_is_free(self):
        token = self.token(self.ad)
        Ad.objects.filter(pk=self.ad.pk).update(status=AdStatus.ON_HOLD)  # поставили на паузу после выдачи
        self.reload()
        self.assertEqual(self.sent(1, self.view(token)), (1, 0))
        self.assertEqual(self.budget_left(self.ad), 10_000_000)
        self.flush()
        stat = self.stat(self.ad)
        self.assertEqual((stat.views, stat.free_views, stat.spent), (0, 1, Decimal('0')))

    def test_click_window_5_seconds(self):
        first, second, third = (self.token(self.ad) for _ in range(3))
        self.assertEqual(self.sent(1, self.click(first)), (1, 0))   # клик без показа — тоже считаем
        self.assertEqual(self.sent(1, self.click(second)), (0, 1))  # тот же зритель через миг — нет
        self.assertEqual(self.sent(2, self.click(self.token(self.ad, 2))), (1, 0))  # другой зритель — да
        click_key = S.click_key(self.ad.pk, tokens.viewer_key(1))
        self.assertTrue(0 < self.r.ttl(click_key) <= 5)
        self.r.delete(click_key)  # прошло 5 секунд
        self.assertEqual(self.sent(1, self.click(third)), (1, 0))
        self.assertEqual(self.budget_left(self.ad), 10_000_000)  # клики бесплатные
        self.flush()
        self.assertEqual(self.stat(self.ad).clicks, 3)

    def test_batch_rules(self):
        token = self.token(self.ad)
        self.assertEqual(self.send(1, *[self.click(token)] * 51).status_code, 400)
        self.assertEqual(self.send(1).status_code, 400)
        response = self.client.post('/api/ads/events/', json.dumps({'user_id': 1, 'events': 'x'}),
                                    content_type='application/json', HTTP_X_API_KEY=API_KEY)
        self.assertEqual(response.status_code, 400)
        # Каждое событие — отдельно: мусор отклоняется, остальное принимается
        self.assertEqual(self.sent(1, 'junk', {'type': 'view'}, {'random_id': token, 'type': 'like'},
                                   self.view(token), self.click(token)), (2, 3))


class FlushTests(ServingBase):
    """Сброс в Postgres: суммы, повторный запуск пачки, остановка, пополнение, смена CPM"""

    def show(self, ad, *user_ids):
        for user_id in user_ids:
            self.assertEqual(self.sent(user_id, self.view(self.token(ad, user_id))), (1, 0))

    def test_sums_match(self):
        ad = self.active_ad(cpm=1500, views_per_user_per_day=0)
        self.reload()
        self.show(ad, 1, 2, 3, 4, 5)
        self.sent(1, self.click(self.token(ad)))
        self.sent(2, self.click(self.token(ad, 2)))
        self.assertEqual(self.flush(), 1)

        ad = self.fresh(ad)
        self.assertEqual(ad.spent, Decimal('7.5'))  # 5 × 1,5 сума
        stat = self.stat(ad)
        self.assertEqual((stat.views, stat.free_views, stat.clicks, stat.spent), (5, 0, 2, Decimal('7.5')))
        self.assertEqual(self.budget_left(ad), S.to_thousandths(ad.remaining_budget()))  # Redis = Postgres
        self.assertEqual(FlushBatch.objects.count(), 1)
        self.assert_redis_clean()
        self.assertEqual(self.flush(), 0)  # нечего сбрасывать

    def test_rerun_after_crash_after_commit_does_not_double(self):
        ad = self.active_ad(views_per_user_per_day=0)
        self.reload()
        self.show(ad, 1, 2, 3)
        with patch.object(flushing, '_cleanup'):  # «упали» после коммита, ключи пачки остались
            self.flush()
        self.assertEqual(self.fresh(ad).spent, Decimal('3'))
        batch_id = self.r.zrange(S.BATCHES, 0, -1)[0]
        self.r.zadd(S.BATCHES, {batch_id: time.time() - 120})  # пачка «старая» — её подберёт следующий сброс

        self.flush()
        self.assertEqual(self.fresh(ad).spent, Decimal('3'))  # не задвоилось
        self.assertEqual(self.stat(ad).views, 3)
        self.assert_redis_clean()

    def test_crash_before_commit_is_recovered_once(self):
        ad = self.active_ad(views_per_user_per_day=0)
        self.reload()
        self.show(ad, 1, 2)
        with patch.object(flushing, '_upsert_stats', side_effect=RuntimeError('упали')):
            with self.assertRaises(RuntimeError):
                self.flush()
        self.assertEqual(self.fresh(ad).spent, Decimal('0'))  # транзакция откатилась
        self.assertFalse(FlushBatch.objects.exists())
        self.assertFalse(self.r.exists(S.FLUSH_LOCK))  # замок снят и при ошибке
        batch_id = self.r.zrange(S.BATCHES, 0, -1)[0]
        self.r.zadd(S.BATCHES, {batch_id: time.time() - 120})

        self.flush()
        self.flush()
        self.assertEqual(self.fresh(ad).spent, Decimal('2'))
        self.assertEqual(self.stat(ad).views, 2)
        self.assert_redis_clean()

    def test_flush_lock(self):
        ad = self.active_ad()
        self.reload()
        self.show(ad, 1)
        self.r.set(S.FLUSH_LOCK, 'other')  # сброс уже идёт в другом процессе
        self.assertEqual(self.flush(), 0)
        self.assertEqual(self.fresh(ad).spent, Decimal('0'))
        self.r.delete(S.FLUSH_LOCK)
        self.assertEqual(self.flush(), 1)

    def test_stop_at_budget_end_and_top_up_returns_to_serving(self):
        ad = self.active_ad(cpm=5_000_000, views_per_user_per_day=0)  # 5000 сумов за показ
        self.reload()
        self.show(ad, 1, 2)
        self.flush()
        ad = self.fresh(ad)
        self.assertEqual((ad.status, ad.spent), (AdStatus.STOPPED, Decimal('10000')))
        self.reload()
        self.assertIsNone(self.serve_ad(3))

        with self.captureOnCommitCallbacks(execute=True):
            response = self.owner_client.post(f'/api/ads/{ad.pk}/budget/', {'amount': '10000'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        ad = self.fresh(ad)
        self.assertEqual((ad.status, ad.budget), (AdStatus.ACTIVE, Decimal('20000')))
        self.assertEqual(self.budget_left(ad), 10_000_000)
        self.reload()
        served = self.serve_ad(3)
        self.assertIsNotNone(served)
        self.assertEqual(self.sent(3, self.view(served['random_id'])), (1, 0))
        self.assertEqual(self.budget_left(ad), 5_000_000)

    def test_top_up_records_unflushed_views(self):
        ad = self.active_ad(views_per_user_per_day=0)
        self.reload()
        self.show(ad, 1, 2, 3)  # не сброшены
        with self.captureOnCommitCallbacks(execute=True):
            response = self.owner_client.post(f'/api/ads/{ad.pk}/budget/', {'amount': '5000'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        ad = self.fresh(ad)
        self.assertEqual((ad.budget, ad.spent), (Decimal('15000'), Decimal('3')))
        self.assertEqual(self.budget_left(ad), 14_997_000)  # ключ пересчитан из Postgres
        self.assertFalse(self.r.exists(S.frozen_key(ad.pk)))
        self.show(ad, 4)
        self.flush()
        self.assertEqual(self.fresh(ad).spent, Decimal('4'))
        self.assertEqual(self.stat(ad).views, 4)
        self.assert_redis_clean()

    def test_cpm_change_applies_to_new_views(self):
        ad = self.active_ad(views_per_user_per_day=0)
        self.reload()
        old_token = self.token(ad, 1)
        self.show(ad, 2)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.owner_client.patch(f'/api/ads/{ad.pk}/', {'cpm': 2500}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.reload()
        self.assertEqual(self.sent(1, self.view(old_token)), (1, 0))  # новая цена — к новым показам
        self.assertEqual(self.budget_left(ad), 10_000_000 - 1000 - 2500)
        self.flush()
        self.assertEqual(self.fresh(ad).spent, Decimal('3.5'))

    def test_overspend_is_clamped_and_reported_once(self):
        boss = User.objects.create_superuser('boss', 'boss@test.uz', 'pass12345')
        ad = self.active_ad()
        day = S.day_of()
        for extra in (5000, 7000):
            # Такого быть не должно: трата сверх бюджета (например, руками поправили Redis)
            self.r.hincrby(S.delta_key(ad.pk), f'{day}:v', 1)
            self.r.hincrby(S.delta_key(ad.pk), f'{day}:s', 10_000_000 + extra)
            self.r.sadd(S.DIRTY, str(ad.pk))
            with self.assertLogs('apps.ads.serving.flush', 'ERROR'):
                self.flush()
            ad = self.fresh(ad)
            self.assertEqual(ad.spent, Decimal('10000'))  # обрезали до остатка
            self.assertEqual(self.stat(ad).spent, Decimal('10000'))
        alerts = Notification.objects.filter(user=boss, type=Notification.Type.MONEY_ALERT)
        self.assertEqual(alerts.count(), 1)  # один раз на объявление
        self.assertIn(str(ad.pk), alerts.get().body)


class RefundTests(ServingBase):
    """Возврат после показов: сначала записать показанное, потом вернуть остаток"""

    def balance(self):
        return Balance.objects.get(user=self.owner).amount

    def test_block_refunds_only_unshown(self):
        ad = self.active_ad(views_per_user_per_day=0)
        self.reload()
        old = self.token(ad, 9)
        for user_id in (1, 2, 3):
            self.sent(user_id, self.view(self.token(ad, user_id)))  # показы ещё не сброшены
        before = self.balance()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.mod_client.post(f'/api/moderation/ads/{ad.pk}/block/', {'reason': 'спам'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['refund_amount'], '9997.00')
        ad = self.fresh(ad)
        self.assertEqual((ad.status, ad.spent, ad.refunded), (AdStatus.BLOCKED, Decimal('3'), Decimal('9997')))
        self.assertEqual(self.balance(), before + Decimal('9997'))
        self.assertIsNone(self.budget_left(ad))  # ключ бюджета снят

        # Показ по токену, выданному до блокировки, — бесплатный
        self.reload()
        self.assertEqual(self.sent(9, self.view(old)), (1, 0))
        self.flush()
        ad = self.fresh(ad)
        self.assertEqual((ad.spent, ad.refunded), (Decimal('3'), Decimal('9997')))
        stat = self.stat(ad)
        self.assertEqual((stat.views, stat.free_views), (3, 1))
        self.assert_redis_clean()

    def test_delete_with_batch_in_flight(self):
        ad = self.active_ad(views_per_user_per_day=0)
        self.reload()
        for user_id in (1, 2):
            self.sent(user_id, self.view(self.token(ad, user_id)))
        # Сброс успел забрать дельты в пачку, но ещё не записал их
        batch_id = 'a' * 32
        S.store().grab(keys=[S.DIRTY, S.batch_key(batch_id), S.BATCHES],
                       args=[batch_id, time.time() - 120, S.PREFIX, 500])
        self.sent(3, self.view(self.token(ad, 3)))  # и ещё показ после этого

        with self.captureOnCommitCallbacks(execute=True):
            response = self.owner_client.delete(f'/api/ads/{ad.pk}/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['refund_amount'], '9997.00')
        ad = self.fresh(ad)
        self.assertEqual((ad.status, ad.spent, ad.refunded), (AdStatus.DELETED, Decimal('3'), Decimal('9997')))

        self.flush()  # подбирает «старую» пачку — она уже пустая
        self.assertEqual(self.fresh(ad).spent, Decimal('3'))
        self.assertEqual(self.stat(ad).views, 3)
        self.assert_redis_clean()

    def test_redis_down_moves_no_money(self):
        ad = self.active_ad()
        before = self.balance()
        with override_settings(ADS_REDIS_URL=REDIS_DOWN):
            block = self.mod_client.post(f'/api/moderation/ads/{ad.pk}/block/', {'reason': 'спам'}, format='json')
            delete = self.owner_client.delete(f'/api/ads/{ad.pk}/')
            top_up = self.owner_client.post(f'/api/ads/{ad.pk}/budget/', {'amount': '5000'}, format='json')
        self.assertEqual((block.status_code, delete.status_code, top_up.status_code), (503, 503, 503))
        ad = self.fresh(ad)
        self.assertEqual((ad.status, ad.budget, ad.refunded), (AdStatus.ACTIVE, Decimal('10000'), Decimal('0')))
        self.assertEqual(self.balance(), before)

    def test_decline_does_not_need_redis(self):
        response = create_ad(self.owner_client, Placement.SEARCH, targeting={'queries': [QUERY]})
        with override_settings(ADS_REDIS_URL=REDIS_DOWN):
            declined = self.mod_client.post(f'/api/moderation/ads/{response.data["id"]}/decline/',
                                            {'reason': 'нет'}, format='json')
        self.assertEqual(declined.status_code, 200, declined.data)  # на модерации не показывалось


class HotPathTests(ServingBase):
    """Ноль SQL на горячем пути, версия снимка, ключи бюджета"""

    def test_zero_sql_on_serve_and_events(self):
        self.active_ad()
        self.reload()
        with self.assertNumQueries(0):
            served = self.serve_ad()
            self.assertEqual(self.sent(1, self.view(served['random_id']), self.click(served['random_id'])), (2, 0))

    def test_version_bump_reloads_snapshot(self):
        ad = self.active_ad()
        old = self.reload()
        snapshot.refresh_if_needed()
        self.assertIs(snapshot._current, old)  # версия та же, 30 с не прошло — не перечитываем

        with self.captureOnCommitCallbacks(execute=True):
            self.owner_client.patch(f'/api/ads/{ad.pk}/', {'cpm': 4000}, format='json')
        snapshot.refresh_if_needed()
        self.assertIsNot(snapshot._current, old)
        self.assertEqual(snapshot._current.ads[str(ad.pk)].price, 4000)

    def test_reload_every_30_seconds(self):
        self.active_ad()
        old = self.reload()
        old.loaded_at -= snapshot.RELOAD_EVERY
        snapshot.refresh_if_needed()
        self.assertIsNot(snapshot._current, old)

    def test_budget_key_created_once_and_not_while_frozen(self):
        ad = self.active_ad()
        self.reload()
        self.assertEqual(self.budget_left(ad), 10_000_000)
        self.r.set(S.budget_key(ad.pk), 5)
        self.reload()
        self.assertEqual(self.budget_left(ad), 5)  # SET NX: рабочую копию не перезаписываем

        self.r.delete(S.budget_key(ad.pk))
        self.r.set(S.frozen_key(ad.pk), 'mark')  # идёт возврат или пополнение
        self.reload()
        self.assertIsNone(self.budget_left(ad))
        self.r.delete(S.frozen_key(ad.pk))
        self.reload()
        self.assertEqual(self.budget_left(ad), 10_000_000)


class RaceTests(ServingBase):
    """Параллельные показы одного объявления в потоках на настоящем Redis"""

    THREADS, VIEWS_EACH = 16, 25

    def run_viewers(self, ad, during=None):
        """Потоки шлют показы (у каждого потока свой зритель) — пока главный поток делает during()"""
        snap = snapshot._current
        start = threading.Barrier(self.THREADS + 1)
        errors = []

        def viewer(user_id):
            try:
                key = tokens.viewer_key(user_id)
                start.wait()
                for _ in range(self.VIEWS_EACH):
                    events.process(snap, key, [self.view(self.token(ad, user_id))])
            except Exception as exc:  # pragma: no cover — упавший поток должен уронить тест
                errors.append(exc)

        threads = [threading.Thread(target=viewer, args=(user_id,)) for user_id in range(1, self.THREADS + 1)]
        for thread in threads:
            thread.start()
        start.wait()
        if during:
            during()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])

    def test_parallel_views_never_overspend(self):
        ad = self.active_ad(cpm=1_000_000, views_per_user_per_day=0)  # 1000 сумов за показ — бюджета на 10
        self.reload()

        def flush_meanwhile():
            for _ in range(5):
                self.flush()

        self.run_viewers(ad, flush_meanwhile)
        self.flush()

        ad = self.fresh(ad)
        stat = self.stat(ad)
        total = self.THREADS * self.VIEWS_EACH
        self.assertEqual(ad.spent, Decimal('10000'))  # ровно бюджет, не больше
        self.assertEqual((stat.views, stat.free_views, stat.spent), (10, total - 10, Decimal('10000')))
        self.assertEqual(self.budget_left(ad), 0)
        self.assertEqual(self.fresh(ad).status, AdStatus.STOPPED)
        self.assert_redis_clean()

    def test_parallel_views_with_block_in_the_middle(self):
        ad = self.active_ad(cpm=50_000, views_per_user_per_day=0)  # 50 сумов за показ — бюджета на 200
        self.reload()
        before = Balance.objects.get(user=self.owner).amount

        def block_meanwhile():
            deadline = time.monotonic() + 5
            while self.budget_left(ad) == 10_000_000 and time.monotonic() < deadline:
                time.sleep(0.001)  # блокируем, когда платные показы уже идут
            with self.captureOnCommitCallbacks(execute=True):
                response = self.mod_client.post(f'/api/moderation/ads/{ad.pk}/block/', {'reason': 'x'}, format='json')
            self.assertEqual(response.status_code, 200, response.data)

        self.run_viewers(ad, block_meanwhile)
        self.flush()

        ad = self.fresh(ad)
        stat = self.stat(ad)
        self.assertEqual(ad.status, AdStatus.BLOCKED)
        self.assertGreater(stat.views, 0)
        self.assertEqual(ad.spent + ad.refunded, ad.budget)  # всё сошлось до копейки
        self.assertEqual(ad.spent, stat.views * Decimal('50'))  # списано ровно за платные показы
        self.assertEqual(stat.views + stat.free_views, self.THREADS * self.VIEWS_EACH)
        self.assertEqual(Balance.objects.get(user=self.owner).amount, before + ad.refunded)
        self.assert_redis_clean()


def require_redis():
    try:
        S.store().redis.ping()
    except redis.RedisError:
        raise unittest.SkipTest(f'Нет Redis для тестов выдачи ({settings.ADS_REDIS_URL}) — '
                                f'поднимите его или задайте ADS_REDIS_TEST_URL')


@override_settings(**SECRETS)
class LockOrderTests(TransactionTestCase):
    """Пополнение и удаление одного объявления одновременно: везде «объявление → баланс», без deadlock"""

    @classmethod
    def setUpClass(cls):
        require_redis()
        super().setUpClass()

    @patch('apps.notifications.services._enqueue_push')
    def test_top_up_and_delete_at_once(self, _push):
        S.store().redis.flushdb()
        snapshot._current = None
        owner, moderator = make_local_user('owner'), make_moderator()
        response = create_ad(client_for(owner), Placement.SEARCH, targeting={'queries': [QUERY]})
        ad = Ad.objects.get(pk=response.data['id'])
        with transaction.atomic():
            Ad.objects.select_for_update().get(pk=ad.pk).approve(moderator)
        snap = snapshot.reload()
        for user_id in (1, 2):  # два платных показа, в Postgres ещё не сброшены
            token = tokens.issue(str(ad.pk), tokens.viewer_key(user_id), Placement.SEARCH, b'\0' * 8)
            self.assertEqual(events.process(snap, tokens.viewer_key(user_id), [ServingBase.view(token)]), (1, 0))

        real_lock_balance = billing_services._lock_balance
        top_up_holds_balance = threading.Event()

        def lock_balance(user):
            balance = real_lock_balance(user)
            if threading.current_thread().name == 'top-up':
                # Пополнение держит баланс — даём удалению вклиниться. При старом порядке
                # (баланс → объявление) здесь был deadlock: удаление держит объявление и ждёт баланс
                top_up_holds_balance.set()
                time.sleep(0.5)
            return balance

        results = {}

        def run(name, request):
            try:
                results[name] = request().status_code
            except Exception as exc:  # deadlock и прочие ошибки БД — сюда
                results[name] = repr(exc)
            finally:
                connection.close()

        def top_up():
            run('top_up', lambda: client_for(owner).post(f'/api/ads/{ad.pk}/budget/', {'amount': '5000'},
                                                         format='json'))

        def delete():
            top_up_holds_balance.wait(5)
            run('delete', lambda: client_for(owner).delete(f'/api/ads/{ad.pk}/'))

        with patch.object(billing_services, '_lock_balance', lock_balance):
            threads = [threading.Thread(target=top_up, name='top-up'), threading.Thread(target=delete)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(results, {'top_up': 200, 'delete': 200})
        ad.refresh_from_db()
        self.assertEqual((ad.status, ad.budget, ad.spent, ad.refunded),
                         (AdStatus.DELETED, Decimal('15000'), Decimal('2'), Decimal('14998')))
        self.assertEqual(Balance.objects.get(user=owner).amount, Decimal('1000000') - 10000 - 5000 + 14998)
        journal = sorted((op.kind, op.reason, op.exact_amount) for op in Op.objects.filter(order_id=ad.pk))
        self.assertEqual(journal, sorted([(Op.Kind.DEBIT, Op.Reason.CREATE, Decimal('10000')),
                                          (Op.Kind.DEBIT, Op.Reason.TOP_UP, Decimal('5000')),
                                          (Op.Kind.REFUND, Op.Reason.DELETE, Decimal('14998'))]))


class CeleryQueueTests(SimpleTestCase):
    """Сброс показов и перекодирование видео — в своих очередях, остальное — в очереди по умолчанию"""

    def test_routes(self):
        from core.celery import app

        def queue(task):
            return app.amqp.router.route({}, task)['queue'].name

        self.assertEqual(queue('apps.ads.tasks.flush_ad_stats'), 'serving_flush')
        self.assertEqual(queue('apps.ads.tasks.process_ad_media'), 'media')
        self.assertEqual(queue('apps.billing.tasks.process_money_operations'), 'celery')
        self.assertEqual(queue('apps.ads.tasks.apply_ad_dates'), 'celery')
