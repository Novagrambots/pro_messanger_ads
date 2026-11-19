"""
Тесты выдачи v2 (apps/ads/serving) на настоящем Redis: выбор, токены, события, сброс в Postgres,
заморозка перед возвратом, ноль SQL на горячем пути, гонка параллельных показов в потоках,
порядок блокировок «объявление → баланс», очереди Celery, жалобы зрителей и сервис serving,
место показа BOT (плашка в чате с ботом), CHANNEL_MESSAGE (сообщение в канале) и заработок партнёра,
формат «Пользователи» (Users) в слотах канала, бота и видео, гео зрителя (тестовая база geo-test.mmdb).
Redis — ADS_REDIS_TEST_URL (по умолчанию redis://localhost:6379/15), очищается перед каждым тестом.
"""
import gzip
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import redis
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.ads.models import Ad, AdDailyStat, AdReport, AdStatus, FlushBatch, Placement
from apps.ads.serving import events, flush as flushing, selection, snapshot, store as S, tokens
from apps.ads.serving import views as serving_views
from apps.ads.models import AdMedia, AdPricing
from apps.ads.serving import audience, geo
from apps.ads.tests import (DARYO_ID, KUNUZ_ID, SPORTS, client_for, create_ad, make_local_user,  # noqa: F401
                            setUpModule, tearDownModule)
from apps.billing import services as billing_services
from apps.billing.models import Balance, MoneyOperation as Op
from apps.directories.models import Location, TelegramPeer
from apps.moderation.test_ads import make_moderator
from apps.notifications.models import Notification
from apps.partner.models import ChannelEarning, EarningTransaction
from core import settings as project_settings

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
                                **{'HTTP_X_API_KEY': API_KEY, **headers})

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

    def test_android_never_gets_user_account(self):
        """Android показывает только канал и бота: аккаунт пользователя в его аукционе не участвует"""
        channel = self.active_ad()
        account = self.active_ad()
        peer = TelegramPeer.objects.create(username='shop_admin', peer_type=TelegramPeer.PeerType.USER,
                                           title='Admin', fetched_at=timezone.now())
        Ad.objects.filter(pk=account.pk).update(promoted_peer=peer, url='https://t.me/shop_admin')
        self.reload()

        def picked(platform):
            found = set()
            for user_id in range(1, 60):
                body = {'placement': Placement.SEARCH, 'user_id': user_id, 'context': {'query': QUERY},
                        'platform': platform}
                response = self.client.post('/api/ads/serve/', json.dumps(body), content_type='application/json',
                                            HTTP_X_API_KEY=API_KEY)
                self.assertEqual(response.status_code, 200, response.content)
                ad = response.json()['ads'][0]
                found.add((tokens.decode(ad['random_id']).ad_id, ad['promoted']['type']))
            return found

        self.assertEqual(picked('android'), {(str(channel.pk), 'CHANNEL')})
        self.assertEqual(picked('Android'), {(str(channel.pk), 'CHANNEL')})
        self.assertEqual(picked('ios'), {(str(channel.pk), 'CHANNEL'), (str(account.pk), 'USER')})

    def test_android_gets_nothing_when_only_user_account(self):
        """Только аккаунт пользователя — Android получает «рекламы нет», а не пустое место на экране"""
        account = self.active_ad()
        peer = TelegramPeer.objects.create(username='shop_admin', peer_type=TelegramPeer.PeerType.USER,
                                           title='Admin', fetched_at=timezone.now())
        Ad.objects.filter(pk=account.pk).update(promoted_peer=peer, url='https://t.me/shop_admin')
        self.reload()
        body = {'placement': Placement.SEARCH, 'user_id': 1, 'context': {'query': QUERY}, 'platform': 'android'}
        response = self.client.post('/api/ads/serve/', json.dumps(body), content_type='application/json',
                                    HTTP_X_API_KEY=API_KEY)
        self.assertEqual(response.json()['ads'], [])
        self.assertIsNotNone(self.serve_ad())  # без platform (и у iOS) — как раньше

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
        self.assertEqual(len(item['random_id']), 104)
        self.assertNotIn('cpm', json.dumps(data))

    def test_only_active_ads(self):
        ad = self.active_ad()
        for status in (AdStatus.ON_HOLD, AdStatus.STOPPED, AdStatus.BLOCKED, AdStatus.IN_REVIEW):
            Ad.objects.filter(pk=ad.pk).update(status=status)
            self.reload()
            self.assertIsNone(self.serve_ad(), status)

    def test_placement_is_a_slot(self):
        # В запросе — место в приложении (слот), а не формат: форматы «Пользователи» сервер подбирает сам
        for placement in (Placement.USERS_CHANNEL_MESSAGE, Placement.USERS_VIDEO_BANNER, Placement.USERS_BOT_BANNER):
            self.assertEqual(self.serve(placement=placement).status_code, 400, placement)

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

    def test_several_api_keys(self):
        """Новый и старый ключ работают одновременно — на выдаче, событиях и жалобах"""
        def post(url, key):
            return self.client.post(url, json.dumps({'user_id': 1}), content_type='application/json',
                                    HTTP_X_API_KEY=key)

        with override_settings(AD_SERVER_API_KEYS='new-key,old-key', AD_SERVER_API_KEY=''):
            for key in ('new-key', 'old-key'):
                self.assertEqual(self.serve(HTTP_X_API_KEY=key).status_code, 200, key)
            self.assertEqual(self.serve(HTTP_X_API_KEY=API_KEY).status_code, 403)
            for url in ('/api/ads/serve/', '/api/ads/events/', '/api/ads/report/'):
                self.assertEqual(post(url, 'wrong').status_code, 403, url)
                self.assertEqual(post(url, 'old-key').status_code, 400, url)  # ключ принят, дальше — тело
        with override_settings(AD_SERVER_API_KEYS='', AD_SERVER_API_KEY=''):
            self.assertEqual(self.serve(HTTP_X_API_KEY='').status_code, 403)  # ключей нет — закрыто

    def test_cgnat_one_ip_many_viewers(self):
        """
        Мобильный оператор (CGNAT): тысячи зрителей за одним IP. При лимите на IP по умолчанию
        их не режем (старые 1200 в минуту резали). Лимит на зрителя при этом работает
        """
        self.active_ad()
        self.reload()
        ip = {'HTTP_X_FORWARDED_FOR': '213.230.64.1'}
        with override_settings(ADS_RATE_IP=project_settings.ADS_RATE_IP, ADS_RATE_VIEWER_SERVE=60):
            codes = {self.serve(user_id, **ip).status_code for user_id in range(1, 1502)}
            self.assertEqual(codes, {200})
            self.assertEqual([self.serve(1, **ip).status_code for _ in range(60)][-1], 429)  # зритель 1: 61-й

    def test_rate_limits(self):
        self.active_ad()
        self.reload()
        with override_settings(ADS_RATE_VIEWER_SERVE=2):
            self.assertEqual([self.serve(1).status_code for _ in range(3)], [200, 200, 429])
            self.assertEqual(self.serve(2).status_code, 200)  # у другого зрителя свой счётчик
        self.r.flushdb()
        with override_settings(ADS_RATE_IP=3):
            ip = {'HTTP_X_FORWARDED_FOR': '203.0.113.7, 10.0.0.1'}
            self.assertEqual([self.serve(user_id, **ip).status_code for user_id in (1, 2, 3, 4)], [200, 200, 200, 429])
            self.assertEqual(self.serve(5, HTTP_X_FORWARDED_FOR='203.0.113.8').status_code, 200)
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
        self.assertEqual(len(value), 104)
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

    def test_view_counts_without_thresholds(self):
        # Как у Telegram: view клиент шлёт, когда весь текст на экране; длительность и доля не проверяются
        cases = [{}, {'duration_ms': 1}, {'visible_ratio': 0.1}, {'duration_ms': '2000', 'visible_ratio': 'x'},
                 {'duration_ms': 5000, 'visible_ratio': 1.0}]
        for user_id, extra in enumerate(cases, start=1):
            event = {'random_id': self.token(self.ad, user_id), 'type': 'view', **extra}
            self.assertEqual(self.sent(user_id, event), (1, 0), extra)
        self.assertEqual(self.budget_left(self.ad), 10_000_000 - 1000 * len(cases))

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


class ReportTests(ServingBase):
    """Жалобы зрителей: приём из приложения, очередь модератора, «просмотрено», счётчик"""

    def setUp(self):
        super().setUp()
        self.ad = self.active_ad()

    def report(self, user_id=1, random_id=None, reason='spam', **extra):
        body = {'user_id': user_id, 'random_id': random_id or self.token(self.ad, user_id), 'reason': reason, **extra}
        return self.client.post('/api/ads/report/', json.dumps(body), content_type='application/json',
                                HTTP_X_API_KEY=API_KEY)

    def test_report_once_per_viewer(self):
        response = self.report(123456789, details='  Обман  ')
        self.assertEqual((response.status_code, response.json()), (201, {'reported': True}))
        self.assertEqual(self.report(123456789, reason='other').status_code, 200)  # повтор — не ошибка
        report = AdReport.objects.get()
        self.assertEqual((report.ad_id, report.reason, report.details, report.status),
                         (self.ad.pk, 'spam', 'Обман', AdReport.Status.NEW))
        self.assertEqual(report.viewer_key, tokens.viewer_key(123456789))  # сырой id не храним
        self.assertEqual(self.report(2).status_code, 201)  # другой зритель — своя жалоба
        self.assertEqual(AdReport.objects.count(), 2)

    def test_bad_reports(self):
        self.assertEqual(self.report(reason='boring').status_code, 400)
        self.assertEqual(self.report(details='x' * 501).status_code, 400)
        self.assertEqual(self.report(details=5).status_code, 400)
        self.assertEqual(self.report(user_id=2, random_id=self.token(self.ad, 1)).status_code, 400)  # чужой токен
        self.assertEqual(self.report(random_id='forged').status_code, 400)
        expired = tokens.issue(str(self.ad.pk), tokens.viewer_key(1), Placement.SEARCH, b'\0' * 8,
                               now=time.time() - settings.ADS_TOKEN_TTL - 5)
        self.assertEqual(self.report(random_id=expired).status_code, 400)
        response = self.client.post('/api/ads/report/', '{}', content_type='application/json')
        self.assertEqual(response.status_code, 403)  # без X-API-Key
        self.assertFalse(AdReport.objects.exists())

    def test_report_rate_limit(self):
        other = self.active_ad()
        with override_settings(ADS_RATE_VIEWER_REPORTS=1):
            self.assertEqual(self.report(1).status_code, 201)
            self.assertEqual(self.report(1, random_id=self.token(other, 1)).status_code, 429)

    def test_moderator_queue_grouped_by_ad(self):
        other = self.active_ad()
        for user_id, reason in ((1, 'spam'), (2, 'spam'), (3, 'misleading')):
            self.report(user_id, reason=reason)
        self.report(4, random_id=self.token(other, 4), reason='illegal')

        data = self.mod_client.get('/api/moderation/ads/reports/').data
        self.assertEqual(data['count'], 2)
        first, second = data['results']
        self.assertEqual((first['id'], first['reports_count'], first['reasons']),
                         (str(self.ad.pk), 3, {'spam': 2, 'misleading': 1}))  # больше жалоб — выше
        self.assertEqual(first['owner']['username'], 'owner')
        self.assertEqual(len(first['reports']), 3)
        self.assertNotIn('viewer_key', first['reports'][0])
        self.assertEqual((second['id'], second['reports_count']), (str(other.pk), 1))

        pending = self.mod_client.get('/api/moderation/pending_count/').data
        self.assertEqual(pending['ad_reports'], 4)
        self.assertEqual(pending['total_all'], pending['ads'] + pending['ad_revisions'] + pending['total'] + 4)

        # Просмотрели одну жалобу — она уходит из фильтра NEW
        report_id = first['reports'][0]['id']
        response = self.mod_client.post(f'/api/moderation/ads/reports/{report_id}/review/')
        self.assertEqual((response.status_code, response.data['status']), (200, AdReport.Status.REVIEWED))
        self.assertEqual(self.mod_client.post(f'/api/moderation/ads/reports/{report_id}/review/').status_code, 409)
        new = self.mod_client.get('/api/moderation/ads/reports/?status=NEW').data['results']
        self.assertEqual([(group['id'], group['reports_count']) for group in new],
                         [(str(self.ad.pk), 2), (str(other.pk), 1)])
        reviewed = self.mod_client.get('/api/moderation/ads/reports/?status=REVIEWED').data['results']
        self.assertEqual(reviewed[0]['reports'][0]['reviewed_by'], self.moderator.username)
        self.assertEqual(self.mod_client.get('/api/moderation/pending_count/').data['ad_reports'], 3)

    def test_permissions(self):
        self.report(1)
        report = AdReport.objects.get()
        self.assertEqual(self.owner_client.get('/api/moderation/ads/reports/').status_code, 403)
        self.assertEqual(self.owner_client.post(f'/api/moderation/ads/reports/{report.pk}/review/').status_code, 403)
        # Жалобу на своё объявление модератор разбирает сам
        own = AdReport.objects.create(ad=self.ad, viewer_key='0' * 32, reason='spam')
        Ad.objects.filter(pk=self.ad.pk).update(user=self.moderator)
        response = self.mod_client.post(f'/api/moderation/ads/reports/{own.pk}/review/')
        self.assertEqual((response.status_code, response.data['status']), (200, AdReport.Status.REVIEWED))
        self.assertEqual(AdReport.objects.get(pk=own.pk).reviewed_by, self.moderator)


@override_settings(ROOT_URLCONF='core.urls_serving')
class ServingUrlconfTests(ServingBase):
    """Сервис serving (SERVING_ONLY=True) отдаёт только горячий путь"""

    def test_only_hot_path(self):
        with override_settings(ROOT_URLCONF='core.urls'):  # объявление создаём через кабинет и модерацию
            ad = self.active_ad()
        self.reload()
        served = self.serve_ad()
        self.assertIsNotNone(served)
        self.assertEqual(self.sent(1, self.view(served['random_id'])), (1, 0))
        report = self.client.post('/api/ads/report/', json.dumps({
            'user_id': 1, 'random_id': served['random_id'], 'reason': 'spam'}),
            content_type='application/json', HTTP_X_API_KEY=API_KEY)
        self.assertEqual(report.status_code, 201)
        for path in ('/api/ads/', f'/api/ads/{ad.pk}/', '/admin/', '/api/moderation/ads/reports/', '/api/docs/'):
            self.assertEqual(self.client.get(path).status_code, 404, path)


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


class BotPlacementTests(ServingBase):
    """Место показа BOT: выбранные боты и «любые боты», аукцион между ними, свой бот не в своём чате"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for username in ('newsbot', 'kinobot'):  # боты в кэше TelegramPeer — тесты не ходят в Telegram
            TelegramPeer.objects.create(username=username, peer_type=TelegramPeer.PeerType.BOT, title=username,
                                        fetched_at=timezone.now())

    def bot_ad(self, bots=(), cpm=1000, url='https://t.me/kunuz', **overrides):
        response = create_ad(self.owner_client, Placement.BOT, cpm=cpm, url=url,
                             targeting={'bots': list(bots)}, **overrides)
        self.assertEqual(response.status_code, 201, response.data)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.mod_client.post(f'/api/moderation/ads/{response.data["id"]}/approve/').status_code,
                             200)
        return Ad.objects.get(pk=response.data['id'])

    def serve_bot(self, bot_username, user_id=1):
        body = {'placement': 'BOT', 'user_id': user_id, 'context': {'bot_username': bot_username}}
        return self.client.post('/api/ads/serve/', json.dumps(body), content_type='application/json',
                                HTTP_X_API_KEY=API_KEY)

    def served_in(self, bot_username, user_id=1):
        """id объявления, выданного в чате бота, или None"""
        response = self.serve_bot(bot_username, user_id)
        self.assertEqual(response.status_code, 200, response.content)
        ads = response.json()['ads']
        return tokens.decode(ads[0]['random_id']).ad_id if ads else None

    def test_only_in_selected_bots(self):
        ad = self.bot_ad(bots=['t.me/weatherbot', '@newsbot'])
        self.reload()
        self.assertEqual(self.served_in('weatherbot'), str(ad.pk))
        self.assertEqual(self.served_in('newsbot'), str(ad.pk))
        self.assertIsNone(self.served_in('kinobot'))
        self.assertIsNone(self.served_in('otherbot'))

    def test_any_bot_when_none_selected(self):
        ad = self.bot_ad(bots=[])
        self.reload()
        for bot in ('weatherbot', 'kinobot', 'somebodyelsesbot'):
            self.assertEqual(self.served_in(bot), str(ad.pk), bot)

    def test_selected_and_any_bot_compete_in_one_auction(self):
        targeted = self.bot_ad(bots=['weatherbot'], cpm=1000)
        anywhere = self.bot_ad(cpm=2000)
        self.reload()
        self.assertEqual(self.served_in('weatherbot', 1), str(anywhere.pk))  # «любые боты» дороже
        self.assertEqual(self.served_in('kinobot', 1), str(anywhere.pk))
        # Зрителю 1 дорогое уже показали (лимит 1 в сутки) — в чате weatherbot идёт следующее по ставке
        token = self.serve_bot('weatherbot', 1).json()['ads'][0]['random_id']
        self.assertEqual(self.sent(1, self.view(token)), (1, 0))
        self.assertEqual(self.served_in('weatherbot', 1), str(targeted.pk))
        self.assertIsNone(self.served_in('kinobot', 1))  # там кроме «любых ботов» никого

        Ad.objects.filter(pk=targeted.pk).update(cpm=3000)  # выбравшее бота стало дороже
        self.reload()
        self.assertEqual(self.served_in('weatherbot', 2), str(targeted.pk))

    def test_bot_username_forms(self):
        ad = self.bot_ad(bots=['weatherbot'])
        self.reload()
        for form in ('weatherbot', 'WeatherBot', '@WEATHERBOT', 't.me/weatherbot', 'https://t.me/WeatherBot',
                     ' @weatherbot '):
            self.assertEqual(self.served_in(form), str(ad.pk), form)
        for bad in ('', '   ', 'we ather bot', 'a' * 40 + 'bot', 123, None, ['weatherbot']):
            self.assertEqual(self.serve_bot(bad).status_code, 400, bad)
        self.assertIsNone(self.served_in('weather'))  # имя без «bot» — не ошибка, просто тут рекламы нет

    def test_bot_without_bot_suffix(self):
        # @BotFather — бот, хоть имя и не кончается на «bot»: и выбрать его можно, и показ в нём идёт
        ad = self.bot_ad(bots=['@BotFather'])
        self.reload()
        self.assertEqual(self.served_in('BotFather'), str(ad.pk))
        self.assertIsNone(self.served_in('weatherbot'))

    def test_own_bot_not_advertised_in_its_chat(self):
        own = self.bot_ad(url='https://t.me/weatherbot')  # продвигает weatherbot, боты не выбраны
        self.reload()
        self.assertIsNone(self.served_in('weatherbot'))
        self.assertEqual(self.served_in('kinobot'), str(own.pk))
        # Даже если сам выбрал свой бот
        Ad.objects.filter(pk=own.pk).update(targeting={'bots': ['weatherbot', 'kinobot']})
        self.reload()
        self.assertIsNone(self.served_in('weatherbot'))
        self.assertEqual(self.served_in('kinobot'), str(own.pk))

    def test_show_channel_picture(self):
        plain = self.bot_ad(bots=['weatherbot'])
        TelegramPeer.objects.filter(username='kunuz').update(avatar='tg_avatars/kunuz_abcd1234.jpg')
        self.reload()
        item = self.serve_bot('weatherbot').json()['ads'][0]
        self.assertEqual(set(item), {'random_id', 'placement', 'text', 'url', 'promoted', 'button_type', 'media',
                                     'other_info', 'label'})
        self.assertEqual((item['placement'], item['text'], item['url'], item['button_type'], item['media'],
                          item['label']), (Placement.BOT, plain.text, plain.url, 'CHANNEL', None, 'Ad'))
        self.assertIsNone(item['promoted']['avatar_url'])  # без «фото канала» — без аватара

        Ad.objects.filter(pk=plain.pk).update(show_channel_picture=True)
        self.reload()
        item = self.serve_bot('weatherbot').json()['ads'][0]
        self.assertEqual(item['promoted']['avatar_url'], 'http://testserver/media/tg_avatars/kunuz_abcd1234.jpg')

    def test_promoted_bot_button(self):
        self.bot_ad(url='https://t.me/newsbot')
        self.reload()
        item = self.serve_bot('kinobot').json()['ads'][0]
        self.assertEqual((item['button_type'], item['promoted']['type'], item['promoted']['username']),
                         ('BOT', 'BOT', 'newsbot'))

    def test_zero_sql_and_events(self):
        ad = self.bot_ad(bots=['weatherbot'])
        self.reload()
        with self.assertNumQueries(0):
            token = self.serve_bot('weatherbot').json()['ads'][0]['random_id']
            self.assertEqual(self.sent(1, self.view(token), self.click(token)), (2, 0))
        self.assertEqual(tokens.decode(token).placement, Placement.BOT)
        self.assertEqual(self.budget_left(ad), 10_000_000 - 1000)
        self.flush()
        stat = self.stat(ad)
        self.assertEqual((stat.views, stat.clicks, stat.spent), (1, 1, Decimal('1')))


GAZETA_ID, OTHER_ID = -1001000000003, -1001000000009  # канал gazeta и канал, которого нет в TelegramPeer
BOOKS, CRYPTO, OTHER = 3, 7, 63  # id тем


class ChannelServingBase(ServingBase):
    """Объявления «сообщение в канале» и каналы партнёрки"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        TelegramPeer.objects.create(username='gazeta', peer_type=TelegramPeer.PeerType.CHANNEL, telegram_id=GAZETA_ID,
                                    title='gazeta', fetched_at=timezone.now())
        cls.partner = make_local_user('partner')

    def channel_ad(self, cpm=1000, budget='10000', **targeting):
        """Объявление продвигает kunuz (он сам автоматически в exclude_channels)"""
        response = create_ad(self.owner_client, Placement.CHANNEL_MESSAGE, cpm=cpm, budget=budget,
                             targeting=targeting or {'languages': ['uz']})
        self.assertEqual(response.status_code, 201, response.data)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.mod_client.post(f'/api/moderation/ads/{response.data["id"]}/approve/').status_code,
                             200)
        return Ad.objects.get(pk=response.data['id'])

    def channel(self, channel_id, language=None, topics=(), confirmed=False, **fields):
        return ChannelEarning.objects.create(
            channel_id=channel_id, language_id=language, topics=list(topics),
            owner=self.partner if confirmed else None,
            claim_status=ChannelEarning.ClaimStatus.CONFIRMED if confirmed else ChannelEarning.ClaimStatus.UNCLAIMED,
            **fields)

    def serve_channel(self, channel_id, user_id=1):
        body = {'placement': 'CHANNEL_MESSAGE', 'user_id': user_id, 'context': {'channel_id': channel_id}}
        return self.client.post('/api/ads/serve/', json.dumps(body), content_type='application/json',
                                HTTP_X_API_KEY=API_KEY)

    def served_in(self, channel_id, user_id=1):
        response = self.serve_channel(channel_id, user_id)
        self.assertEqual(response.status_code, 200, response.content)
        ads = response.json()['ads']
        return tokens.decode(ads[0]['random_id']).ad_id if ads else None

    def view_in(self, channel_id, user_id):
        """Выдать в канале и прислать засчитанный показ. Возвращает (принято, отклонено)"""
        token = self.serve_channel(channel_id, user_id).json()['ads'][0]['random_id']
        return self.sent(user_id, self.view(token))


class ChannelPlacementTests(ChannelServingBase):
    """Кто подходит: «список каналов» и «языки + темы», исключения, свой канал, формат ответа"""

    def test_specific_channels_mode(self):
        ad = self.channel_ad(channels=['daryo'])
        self.channel(GAZETA_ID, language='uz')  # по языку подошёл бы, но режим — список каналов
        self.reload()
        self.assertEqual(self.served_in(DARYO_ID), str(ad.pk))
        self.assertEqual(self.served_in(str(DARYO_ID)), str(ad.pk))  # строкой тоже можно
        self.assertIsNone(self.served_in(GAZETA_ID))
        self.assertIsNone(self.served_in(OTHER_ID))

    def test_languages_mode(self):
        ad = self.channel_ad(languages=['uz', 'ru'])
        self.channel(DARYO_ID, language='uz', topics=[BOOKS])
        self.channel(GAZETA_ID, language='ru')  # без тем — тоже подходит
        self.channel(OTHER_ID, language='en', topics=[BOOKS])
        self.reload()
        self.assertEqual(self.served_in(DARYO_ID), str(ad.pk))
        self.assertEqual(self.served_in(GAZETA_ID), str(ad.pk))
        self.assertIsNone(self.served_in(OTHER_ID))  # язык не тот

    def test_topics_and_exclude_topics(self):
        ad = self.channel_ad(languages=['uz'], topics=[SPORTS, BOOKS], exclude_topics=[CRYPTO])
        self.channel(DARYO_ID, language='uz', topics=[SPORTS])
        self.channel(GAZETA_ID, language='uz', topics=[OTHER])  # темы не пересекаются
        self.channel(OTHER_ID, language='uz', topics=[BOOKS, CRYPTO])  # подошла бы, но есть исключённая тема
        self.channel(-1001000000010, language='uz')  # без тем, а объявление темы выбрало
        self.reload()
        self.assertEqual(self.served_in(DARYO_ID), str(ad.pk))
        for channel_id in (GAZETA_ID, OTHER_ID, -1001000000010):
            self.assertIsNone(self.served_in(channel_id), channel_id)

    def test_channel_without_meta(self):
        by_language = self.channel_ad(languages=['uz'])
        by_list = self.channel_ad(channels=['gazeta'], cpm=1500)
        self.channel(DARYO_ID)  # в партнёрке, но без языка
        self.reload()
        self.assertIsNone(self.served_in(DARYO_ID))   # без языка в «языки + темы» не попадает
        self.assertIsNone(self.served_in(OTHER_ID))   # канала нет в партнёрке совсем
        self.assertEqual(self.served_in(GAZETA_ID), str(by_list.pk))  # явный выбор работает и без метаданных
        self.assertIsNotNone(by_language)

    def test_exclude_channels_and_own_channel(self):
        ad = self.channel_ad(languages=['uz'], exclude_channels=['daryo'])
        for channel_id in (DARYO_ID, GAZETA_ID, KUNUZ_ID):
            self.channel(channel_id, language='uz')
        self.reload()
        self.assertIsNone(self.served_in(DARYO_ID))   # исключён
        self.assertIsNone(self.served_in(KUNUZ_ID))   # реклама kunuz не показывается в самом kunuz
        self.assertEqual(self.served_in(GAZETA_ID), str(ad.pk))
        # Даже если автоисключение из таргетинга убрали — выдача сверяет id продвигаемого канала
        targeting = dict(ad.targeting, exclude_channels=[])
        Ad.objects.filter(pk=ad.pk).update(targeting=targeting)
        self.reload()
        self.assertIsNone(self.served_in(KUNUZ_ID))
        self.assertEqual(self.served_in(DARYO_ID), str(ad.pk))

    def test_bad_channel_id(self):
        self.channel_ad()
        self.reload()
        for bad in ('', 'abc', 123, -1001, '-1001', '-2001000000002', '1001000000002', True, None, 1.5,
                    '-100' + '1' * 14, [DARYO_ID]):
            self.assertEqual(self.serve_channel(bad).status_code, 400, bad)
        self.assertEqual(self.serve_channel(' -1001000000002 ').status_code, 200)

    def test_response_media_picture_and_token(self):
        ad = self.channel_ad(channels=['daryo'])
        media = AdMedia.objects.create(user=self.owner, file='ads/media/x.mp4', media_type=AdMedia.MediaType.VIDEO,
                                       width=1280, height=720, duration=Decimal('12.50'), status=AdMedia.Status.READY)
        Ad.objects.filter(pk=ad.pk).update(media=media)
        self.reload()
        item = self.serve_channel(DARYO_ID).json()['ads'][0]
        self.assertEqual(item['media'], {'type': 'VIDEO', 'url': 'http://testserver/media/ads/media/x.mp4',
                                         'width': 1280, 'height': 720, 'duration': '12.50'})
        self.assertEqual((item['placement'], item['button_type'], item['promoted']['avatar_url']),
                         (Placement.CHANNEL_MESSAGE, 'CHANNEL', None))
        self.assertEqual(len(item['random_id']), 104)
        self.assertEqual(tokens.decode(item['random_id']).channel_id, DARYO_ID)  # канал — в токене

        TelegramPeer.objects.filter(username='kunuz').update(avatar='tg_avatars/kunuz_abcd1234.jpg')
        Ad.objects.filter(pk=ad.pk).update(media=None, show_channel_picture=True)
        self.reload()
        item = self.serve_channel(DARYO_ID).json()['ads'][0]
        self.assertEqual(item['promoted']['avatar_url'], 'http://testserver/media/tg_avatars/kunuz_abcd1234.jpg')

    def test_zero_sql(self):
        self.channel_ad(languages=['uz'])
        self.channel(DARYO_ID, language='uz', confirmed=True)
        self.reload()
        with self.assertNumQueries(0):
            token = self.serve_channel(DARYO_ID).json()['ads'][0]['random_id']
            self.assertEqual(self.sent(1, self.view(token), self.click(token)), (2, 0))


class PartnerEarningTests(ChannelServingBase):
    """Заработок партнёра: только оплаченные показы в подтверждённом канале, точно, без задвоения"""

    def earning(self, channel_id):
        return ChannelEarning.objects.get(channel_id=channel_id)

    def test_only_paid_views_in_confirmed_channel(self):
        ad = self.channel_ad(cpm=1001, languages=['uz'])
        Ad.objects.filter(pk=ad.pk).update(views_per_user_per_day=0)
        self.channel(DARYO_ID, language='uz', confirmed=True)   # партнёр, доля по умолчанию 0.5
        self.channel(GAZETA_ID, language='uz')                  # не подтверждён — без заработка
        self.reload()
        for user_id in (1, 2, 3):
            self.assertEqual(self.view_in(DARYO_ID, user_id), (1, 0))
        self.assertEqual(self.view_in(GAZETA_ID, 4), (1, 0))
        self.sent(1, self.click(self.token(ad)))  # клики бесплатные — заработка нет
        self.flush()

        ad = self.fresh(ad)
        self.assertEqual(ad.spent, Decimal('4.004'))  # 4 платных показа × 1,001
        daryo = self.earning(DARYO_ID)
        # 3 × 1,001 × 0,5 = 1,5015 — точно, без округления
        self.assertEqual((daryo.available, daryo.total_earned, daryo.total_impressions),
                         (Decimal('1.5015'), Decimal('1.5015'), 3))
        self.assertEqual((self.earning(GAZETA_ID).total_earned, self.earning(GAZETA_ID).total_impressions),
                         (Decimal('0'), 0))
        accrual = EarningTransaction.objects.get(channel=daryo, ad=ad)
        self.assertEqual((accrual.kind, accrual.amount, accrual.impressions, accrual.order),
                         (EarningTransaction.Kind.ACCRUAL, Decimal('1.5015'), 3, None))

    def test_free_views_earn_nothing(self):
        ad = self.channel_ad(cpm=5_000_000, languages=['uz'])  # 5000 сумов за показ — бюджета на 2
        Ad.objects.filter(pk=ad.pk).update(views_per_user_per_day=0)
        self.channel(DARYO_ID, language='uz', confirmed=True)
        self.reload()
        for user_id in (1, 2, 3):
            # Токены как из выдачи в канале daryo (после конца бюджета выдача объявление уже не отдаёт)
            token = tokens.issue(str(ad.pk), tokens.viewer_key(user_id), Placement.CHANNEL_MESSAGE, b'\0' * 8,
                                 channel_id=DARYO_ID)
            self.assertEqual(self.sent(user_id, self.view(token)), (1, 0))  # третий — бесплатный
        self.flush()
        daryo = self.earning(DARYO_ID)
        self.assertEqual((daryo.total_earned, daryo.total_impressions), (Decimal('5000'), 2))  # 2 × 5000 × 0,5
        self.assertEqual(self.stat(ad).free_views, 1)

    def test_channel_share_rate_and_accumulation(self):
        ad = self.channel_ad(cpm=1001, languages=['uz'])
        Ad.objects.filter(pk=ad.pk).update(views_per_user_per_day=0)
        self.channel(DARYO_ID, language='uz', confirmed=True, share_rate=Decimal('0.333'))
        self.reload()
        self.view_in(DARYO_ID, 1)
        self.flush()
        self.view_in(DARYO_ID, 2)
        self.flush()  # вторая пачка добавляется в тот же агрегат (канал, объявление)
        daryo = self.earning(DARYO_ID)
        self.assertEqual(daryo.total_earned, Decimal('0.666666'))  # 2 × 1,001 × 0,333
        accrual = EarningTransaction.objects.get(channel=daryo, ad=ad)
        self.assertEqual((accrual.amount, accrual.impressions), (Decimal('0.666666'), 2))
        self.assertEqual(EarningTransaction.objects.count(), 1)

    def test_share_rate_with_too_many_digits(self):
        with override_settings(PARTNER_SHARE_RATE='0.55555'), self.assertLogs('apps.ads.serving.snapshot', 'ERROR'):
            self.assertEqual(snapshot.share_permille(settings.PARTNER_SHARE_RATE), 555)  # вниз до 3 знаков
        self.assertEqual(snapshot.share_permille('0.5'), 500)
        self.assertEqual(snapshot.share_permille(Decimal('1')), 1000)

    def test_rerun_after_crash_does_not_double_earning(self):
        ad = self.channel_ad(languages=['uz'])
        Ad.objects.filter(pk=ad.pk).update(views_per_user_per_day=0)
        self.channel(DARYO_ID, language='uz', confirmed=True)
        self.reload()
        self.view_in(DARYO_ID, 1)
        self.view_in(DARYO_ID, 2)
        with patch.object(flushing, '_cleanup'):  # «упали» после коммита — ключи пачки остались
            self.flush()
        batch_id = self.r.zrange(S.BATCHES, 0, -1)[0]
        self.r.zadd(S.BATCHES, {batch_id: time.time() - 120})
        self.flush()
        daryo = self.earning(DARYO_ID)
        self.assertEqual((daryo.total_earned, daryo.total_impressions), (Decimal('1'), 2))
        self.assertEqual(EarningTransaction.objects.get(channel=daryo, ad=ad).amount, Decimal('1'))
        self.assert_redis_clean()

    def test_block_records_unflushed_earning(self):
        ad = self.channel_ad(languages=['uz'])
        self.channel(DARYO_ID, language='uz', confirmed=True)
        self.reload()
        self.view_in(DARYO_ID, 1)  # не сброшено
        with self.captureOnCommitCallbacks(execute=True):
            response = self.mod_client.post(f'/api/moderation/ads/{ad.pk}/block/', {'reason': 'x'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.earning(DARYO_ID).total_earned, Decimal('0.5'))
        self.assertEqual(self.fresh(ad).spent, Decimal('1'))

    def test_money_format_in_partner_api(self):
        self.channel(DARYO_ID, language='uz', confirmed=True)
        ChannelEarning.objects.filter(channel_id=DARYO_ID).update(available=Decimal('12.35'),
                                                                 total_earned=Decimal('1.5015'))
        item = client_for(self.partner).get('/api/partner/my/').data['results'][0]
        self.assertEqual((item['available'], item['total_earned']), ('12.35', '1.5015'))
        self.assertEqual((item['language'], item['topics'], item['meta_locked']), ('uz', [], False))


class ChannelMetaTests(ChannelServingBase):
    """Язык и темы канала: владелец — свой подтверждённый канал, модератор — любой, выдача обновляется"""

    def owner_patch(self, channel_id, data, user=None):
        return client_for(user or self.partner).patch(f'/api/partner/channels/{channel_id}/', data, format='json')

    def mod_patch(self, channel_id, data):
        return self.mod_client.patch(f'/api/moderation/channels/{channel_id}/', data, format='json')

    def test_owner_sets_meta_of_own_confirmed_channel_only(self):
        self.channel(DARYO_ID, confirmed=True)
        self.channel(GAZETA_ID)  # не заявлен
        response = self.owner_patch(DARYO_ID, {'language': 'uz', 'topics': [SPORTS, BOOKS]})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data['language'], response.data['topics']), ('uz', [SPORTS, BOOKS]))
        daryo = ChannelEarning.objects.get(channel_id=DARYO_ID)
        self.assertEqual((daryo.meta_updated_by, daryo.meta_updated_role), (self.partner, 'OWNER'))
        self.assertEqual(self.owner_patch(DARYO_ID, {'topics': [OTHER]}).data['language'], 'uz')  # можно по одному

        self.assertEqual(self.owner_patch(GAZETA_ID, {'language': 'uz'}).status_code, 404)  # не заявлен
        self.assertEqual(self.owner_patch(DARYO_ID, {'language': 'uz'}, user=self.owner).status_code, 404)  # чужой
        self.assertEqual(self.owner_patch(OTHER_ID, {'language': 'uz'}).status_code, 404)

    def test_validation(self):
        self.channel(DARYO_ID, confirmed=True)
        for data in ({}, {'language': 'xx'}, {'topics': []}, {'topics': [1, 2, 3, 4]}, {'topics': [999]},
                     {'topics': 'sport'}, {'language': None}):
            self.assertEqual(self.owner_patch(DARYO_ID, data).status_code, 400, data)
        self.assertEqual(self.owner_patch(DARYO_ID, {'topics': [SPORTS, SPORTS]}).data['topics'], [SPORTS])

    def test_moderator_fixes_and_owner_is_locked(self):
        self.channel(DARYO_ID, language='ru', confirmed=True)
        response = self.mod_patch(DARYO_ID, {'language': 'uz', 'topics': [SPORTS]})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data['meta_updated_role'], response.data['meta_updated_by']),
                         ('MODERATOR', self.moderator.username))
        response = self.owner_patch(DARYO_ID, {'language': 'ru'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['error'],
                         'Язык и темы проверены модератором, для изменения обратитесь в поддержку')
        self.assertEqual(ChannelEarning.objects.get(channel_id=DARYO_ID).language_id, 'uz')
        self.assertTrue(client_for(self.partner).get('/api/partner/my/').data['results'][0]['meta_locked'])
        self.assertEqual(self.mod_patch(DARYO_ID, {'topics': [BOOKS]}).status_code, 200)  # модератор — может

    def test_moderator_list_and_permissions(self):
        self.channel(DARYO_ID, language='uz', topics=[SPORTS], confirmed=True)
        self.channel(GAZETA_ID, language='uz')       # без тем
        self.channel(OTHER_ID, topics=[SPORTS])      # без языка

        def ids(query=''):
            results = self.mod_client.get(f'/api/moderation/channels/{query}').data['results']
            return {item['channel_id'] for item in results}
        self.assertEqual(ids(), {DARYO_ID, GAZETA_ID, OTHER_ID})
        self.assertEqual(ids('?missing_meta=1'), {GAZETA_ID, OTHER_ID})
        self.assertEqual(ids('?claim_status=CONFIRMED'), {DARYO_ID})
        self.assertEqual(client_for(self.partner).get('/api/moderation/channels/').status_code, 403)
        self.assertEqual(client_for(self.partner).patch(f'/api/moderation/channels/{DARYO_ID}/', {'language': 'ru'},
                                                        format='json').status_code, 403)
        self.assertEqual(self.mod_patch(-1001999999999, {'language': 'uz'}).status_code, 404)
        # Свой канал модератор не правит
        self.channel(-1001000000020)
        ChannelEarning.objects.filter(channel_id=-1001000000020).update(owner=self.moderator)
        self.assertEqual(self.mod_patch(-1001000000020, {'language': 'uz'}).status_code, 403)

    def test_meta_change_updates_serving(self):
        ad = self.channel_ad(languages=['uz'], topics=[SPORTS])
        self.channel(DARYO_ID, confirmed=True)
        self.reload()
        self.assertIsNone(self.served_in(DARYO_ID))
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.owner_patch(DARYO_ID, {'language': 'uz', 'topics': [SPORTS]}).status_code, 200)
        snapshot.refresh_if_needed()  # версия снимка выросла — перечитываем
        self.assertEqual(self.served_in(DARYO_ID), str(ad.pk))
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.mod_patch(DARYO_ID, {'topics': [BOOKS]}).status_code, 200)
        snapshot.refresh_if_needed()
        self.assertIsNone(self.served_in(DARYO_ID))


GEO_FIXTURE = str(Path(__file__).with_name('fixtures') / 'geo-test.mmdb')
# Адреса из тестовой гео-базы (apps/ads/fixtures/make_geo_test_mmdb.py)
IP_ANGREN = '203.0.113.5'      # UZ, город Angren, регион Tashkent Region
IP_TASHKENT = '198.51.100.7'   # UZ, город Tashkent, регион Toshkent Shahri
IP_UZ_SAMARKAND = '100.64.0.9'  # UZ, города нет в справочнике, регион Samarqand Region
IP_ALMATY = '192.0.2.1'        # KZ
POLITICS = audience.POLITICS_TOPIC


def location(name):
    return Location.objects.get(name_en=name).pk


@override_settings(GEOIP_PATH=GEO_FIXTURE)
class UsersBase(ChannelServingBase):
    """Формат «Пользователи»: объявления, запросы слотов и профиль зрителя"""

    def setUp(self):
        super().setUp()
        geo._database = geo._Database()  # база гео — своя у каждого теста (путь подменяется)

    def users_ad(self, placement=Placement.USERS_CHANNEL_MESSAGE, cpm=1000, url='https://t.me/kunuz', **targeting):
        response = create_ad(self.owner_client, placement, cpm=cpm, url=url,
                             targeting={'countries': ['UZ'], **targeting})
        self.assertEqual(response.status_code, 201, response.data)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.mod_client.post(f'/api/moderation/ads/{response.data["id"]}/approve/').status_code,
                             200)
        return Ad.objects.get(pk=response.data['id'])

    def serve_slot(self, slot, context, user_id=1, ip=None, **fields):
        body = {'placement': slot, 'user_id': user_id, 'context': context, **fields}
        headers = {'HTTP_X_FORWARDED_FOR': ip} if ip else {}
        return self.client.post('/api/ads/serve/', json.dumps(body), content_type='application/json',
                                HTTP_X_API_KEY=API_KEY, **headers)

    def served(self, slot, context, **kwargs):
        response = self.serve_slot(slot, context, **kwargs)
        self.assertEqual(response.status_code, 200, response.content)
        ads = response.json()['ads']
        return tokens.decode(ads[0]['random_id']).ad_id if ads else None

    @staticmethod
    def in_channel(channel_id=DARYO_ID):
        return {'channel_id': channel_id}


class UsersGeoTests(UsersBase):
    """Страна (phone_country важнее IP), город и область по IP, нет гео-базы"""

    def test_country_phone_beats_ip(self):
        ad = self.users_ad(countries=['KZ'])
        self.reload()
        slot, where = 'CHANNEL_MESSAGE', self.in_channel()
        self.assertEqual(self.served(slot, where, ip=IP_ANGREN, phone_country='KZ'), str(ad.pk))  # номер важнее IP
        self.assertIsNone(self.served(slot, where, ip=IP_ALMATY, phone_country='uz'))
        self.assertEqual(self.served(slot, where, ip=IP_ALMATY), str(ad.pk))       # без номера — по IP
        self.assertIsNone(self.served(slot, where, ip=IP_ANGREN))
        # Кода нет в справочнике — страна по IP
        self.assertEqual(self.served(slot, where, ip=IP_ALMATY, phone_country='XX'), str(ad.pk))
        self.assertIsNone(self.served(slot, where))  # ни номера, ни гео — страны нет
        for bad in ('UZB', 'U1', '', 5):
            self.assertEqual(self.serve_slot(slot, where, phone_country=bad).status_code, 400, bad)

    def test_city_and_its_region(self):
        angren = self.users_ad(locations=[location('Angren')])
        self.reload()
        where = self.in_channel()
        self.assertEqual(self.served('CHANNEL_MESSAGE', where, ip=IP_ANGREN), str(angren.pk))
        self.assertIsNone(self.served('CHANNEL_MESSAGE', where, ip=IP_TASHKENT))

        Ad.objects.filter(pk=angren.pk).update(targeting=dict(angren.targeting,
                                                               locations=[location('Tashkent Region')]))
        self.reload()
        self.assertEqual(self.served('CHANNEL_MESSAGE', where, ip=IP_ANGREN), str(angren.pk))  # Ангрен — в области
        self.assertIsNone(self.served('CHANNEL_MESSAGE', where, ip=IP_TASHKENT))  # Ташкент — не Ташкентская область

        Ad.objects.filter(pk=angren.pk).update(targeting=dict(angren.targeting,
                                                               locations=[location('Samarqand Region')]))
        self.reload()
        # Города нет в справочнике, но регион из гео-базы совпал с областью
        self.assertEqual(self.served('CHANNEL_MESSAGE', where, ip=IP_UZ_SAMARKAND), str(angren.pk))
        self.assertIsNone(self.served('CHANNEL_MESSAGE', where, ip=IP_ANGREN))

    def test_country_with_and_without_locations(self):
        ad = self.users_ad(countries=['UZ', 'KZ'], locations=[location('Angren')])
        self.reload()
        where = self.in_channel()
        self.assertEqual(self.served('CHANNEL_MESSAGE', where, ip=IP_ALMATY), str(ad.pk))  # для KZ локаций нет
        self.assertIsNone(self.served('CHANNEL_MESSAGE', where, ip=IP_TASHKENT))
        # Страна по номеру UZ, а IP из Казахстана — города UZ не знаем
        self.assertIsNone(self.served('CHANNEL_MESSAGE', where, ip=IP_ALMATY, phone_country='UZ'))

    def test_without_geo_database(self):
        plain = self.users_ad()
        with_city = self.users_ad(locations=[location('Angren')], cpm=2000)
        self.reload()
        where = self.in_channel()
        with override_settings(GEOIP_PATH='/nonexistent/dbip.mmdb'), self.assertLogs('apps.ads.serving.geo', 'WARNING'):
            self.assertEqual(self.served('CHANNEL_MESSAGE', where, ip=IP_ANGREN, phone_country='UZ'), str(plain.pk))
            self.assertIsNone(self.served('CHANNEL_MESSAGE', where, ip=IP_ANGREN))  # страны нет — Users не идут
        self.assertIsNotNone(with_city)

    def test_geo_file_appears_and_is_picked_up(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        path = os.path.join(folder, 'dbip.mmdb')
        with override_settings(GEOIP_PATH=path), self.assertLogs('apps.ads.serving.geo', 'WARNING'):
            self.assertIsNone(geo.lookup(IP_ANGREN))
            shutil.copyfile(GEO_FIXTURE, path + '.tmp')
            os.replace(path + '.tmp', path)
            geo._database.checked_at = 0  # прошла минута — процесс перепроверяет файл
            self.assertEqual(geo.lookup(IP_ANGREN).country, 'UZ')
            self.assertIsNone(geo.lookup('not-an-ip'))
            self.assertIsNone(geo.lookup('8.8.8.8'))

    def test_update_database(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        path = os.path.join(folder, 'dbip.mmdb')
        packed = gzip.compress(Path(GEO_FIXTURE).read_bytes())
        requested = []

        class Response:
            def __init__(self, status, body=b''):
                self.status_code, self.raw = status, io.BytesIO(body)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def raise_for_status(self):
                pass

        def get(url, **kwargs):
            requested.append(url)
            # Файла за этот месяц ещё нет — берётся прошлый
            return Response(404) if '2026-11' in url else Response(200, packed)

        with override_settings(GEOIP_PATH=path), patch('apps.ads.serving.geo.requests.get', side_effect=get):
            month = geo.update_database(now=datetime(2026, 11, 2, tzinfo=ZoneInfo('UTC')))
            self.assertEqual(month, '2026-10')
            self.assertTrue(requested[0].endswith('dbip-city-lite-2026-11.mmdb.gz'))
            self.assertEqual(geo.lookup(IP_ALMATY).country, 'KZ')
            self.assertEqual(os.listdir(folder), ['dbip.mmdb'])  # временный файл убран

        # Битый файл не подменяет рабочий
        with override_settings(GEOIP_PATH=path), patch('apps.ads.serving.geo.requests.get',
                                                       return_value=Response(200, gzip.compress(b'garbage'))):
            with self.assertRaises(Exception):
                geo.update_database(now=datetime(2026, 12, 2, tzinfo=ZoneInfo('UTC')))
        self.assertEqual(Path(path).read_bytes(), Path(GEO_FIXTURE).read_bytes())
        self.assertEqual(os.listdir(folder), ['dbip.mmdb'])

    def test_client_ip(self):
        from django.test import RequestFactory
        factory = RequestFactory()
        request = factory.get('/', HTTP_X_FORWARDED_FOR='1.2.3.4, 10.0.0.1', HTTP_CF_CONNECTING_IP='5.6.7.8')
        self.assertEqual(geo.client_ip(request), '1.2.3.4')
        with override_settings(CLIENT_IP_HEADER='CF-Connecting-IP'):
            self.assertEqual(geo.client_ip(request), '5.6.7.8')
        self.assertEqual(geo.client_ip(factory.get('/')), '127.0.0.1')  # REMOTE_ADDR


class UsersAudienceTests(UsersBase):
    """Язык, устройство, темы интересов и аудитории из истории каналов, исключения, политика"""

    def test_language_and_device(self):
        ad = self.users_ad(languages=['uz'], devices=['android', 'desktop'])
        self.reload()
        where = self.in_channel()
        ok = {'ip': IP_TASHKENT}
        self.assertEqual(self.served('CHANNEL_MESSAGE', where, lang='uz-UZ', platform='Android', **ok), str(ad.pk))
        self.assertEqual(self.served('CHANNEL_MESSAGE', where, lang='UZ', platform='windows', **ok), str(ad.pk))
        self.assertIsNone(self.served('CHANNEL_MESSAGE', where, lang='ru', platform='android', **ok))
        self.assertIsNone(self.served('CHANNEL_MESSAGE', where, lang='uz', platform='iPhone', **ok))
        self.assertIsNone(self.served('CHANNEL_MESSAGE', where, lang='uz', **ok))  # устройство неизвестно
        Ad.objects.filter(pk=ad.pk).update(targeting=dict(ad.targeting, languages=[], devices=[]))
        self.reload()
        self.assertEqual(self.served('CHANNEL_MESSAGE', where, **ok), str(ad.pk))  # пусто — любые

    def test_topics_from_channel_history(self):
        self.channel(DARYO_ID, language='uz', topics=[SPORTS])
        self.channel(GAZETA_ID, language='uz', topics=[BOOKS])
        ad = self.users_ad(Placement.USERS_BOT_BANNER, topics=[SPORTS], exclude_topics=[CRYPTO])
        self.reload()
        bot = {'bot_username': 'weatherbot'}
        self.assertIsNone(self.served('BOT', bot, user_id=1, ip=IP_TASHKENT))  # истории нет — тем нет
        self.served('CHANNEL_MESSAGE', self.in_channel(DARYO_ID), user_id=1, ip=IP_TASHKENT)  # побывал в канале
        self.assertEqual(self.served('BOT', bot, user_id=1, ip=IP_TASHKENT), str(ad.pk))
        self.assertIsNone(self.served('BOT', bot, user_id=2, ip=IP_TASHKENT))  # другой зритель — без истории
        # Исключённая тема среди интересов — не показываем
        ChannelEarning.objects.filter(channel_id=GAZETA_ID).update(topics=[BOOKS, CRYPTO])
        self.reload()
        self.served('CHANNEL_MESSAGE', self.in_channel(GAZETA_ID), user_id=1, ip=IP_TASHKENT)
        self.assertIsNone(self.served('BOT', bot, user_id=1, ip=IP_TASHKENT))

    def test_channel_audiences(self):
        ad = self.users_ad(Placement.USERS_BOT_BANNER, channel_audiences=['daryo'],
                           exclude_channel_audiences=['gazeta'])
        self.reload()
        bot = {'bot_username': 'weatherbot'}
        self.assertIsNone(self.served('BOT', bot, user_id=1, ip=IP_TASHKENT))
        self.served('VIDEO', {'channel_id': DARYO_ID, 'msg_id': 7}, user_id=1, ip=IP_TASHKENT)  # видео — тоже история
        self.assertEqual(self.served('BOT', bot, user_id=1, ip=IP_TASHKENT), str(ad.pk))
        self.served('CHANNEL_MESSAGE', self.in_channel(GAZETA_ID), user_id=1, ip=IP_TASHKENT)
        self.assertIsNone(self.served('BOT', bot, user_id=1, ip=IP_TASHKENT))  # аудитория исключённого канала

    def test_dimensions_combine_with_and(self):
        self.channel(DARYO_ID, language='uz', topics=[SPORTS])
        ad = self.users_ad(languages=['uz', 'ru'], topics=[SPORTS, BOOKS], devices=['ios'],
                           locations=[location('Tashkent')])
        self.reload()
        where = self.in_channel(DARYO_ID)  # запрос в daryo сам кладёт его в историю (тема Sports)
        full = {'ip': IP_TASHKENT, 'lang': 'ru', 'platform': 'ios'}
        self.assertEqual(self.served('CHANNEL_MESSAGE', where, **full), str(ad.pk))  # внутри списков — ИЛИ
        for broken in ({'ip': IP_ANGREN}, {'lang': 'en'}, {'platform': 'android'}):
            self.assertIsNone(self.served('CHANNEL_MESSAGE', where, user_id=1, **{**full, **broken}), broken)

    def test_history_30_days_and_500_channels(self):
        viewer = tokens.viewer_key(1)
        key = S.history_key(viewer)
        now = time.time()
        self.r.zadd(key, {'-1001000000777': now - 31 * 86400})  # старше 30 дней
        self.r.zadd(key, {str(-1001000001000 - n): now - 1000 + n for n in range(500)})
        self.serve_slot('CHANNEL_MESSAGE', self.in_channel(DARYO_ID), ip=IP_TASHKENT)
        members = self.r.zrange(key, 0, -1)
        self.assertEqual(len(members), 500)                # не больше 500
        self.assertNotIn('-1001000000777', members)        # старше 30 дней — отрезано
        self.assertNotIn(str(-1001000001000), members)     # самый старый из 500 вытеснен новым
        self.assertEqual(members[-1], str(DARYO_ID))
        self.assertTrue(0 < self.r.ttl(key) <= 30 * 86400)
        self.assertEqual(self.r.keys(f'{S.PREFIX}h:*'), [key])  # ключ — только viewer_key, сырого id нет

    def test_politics(self):
        self.channel(DARYO_ID, language='uz', topics=[POLITICS, SPORTS])
        self.channel(GAZETA_ID, language='uz', topics=[SPORTS])
        self.channel(OTHER_ID, language='uz')  # темы неизвестны
        only = self.users_ad(politics='only')
        self.reload()
        ip = {'ip': IP_TASHKENT}
        self.assertEqual(self.served('CHANNEL_MESSAGE', self.in_channel(DARYO_ID), **ip), str(only.pk))
        self.assertIsNone(self.served('CHANNEL_MESSAGE', self.in_channel(GAZETA_ID), **ip))
        self.assertIsNone(self.served('CHANNEL_MESSAGE', self.in_channel(OTHER_ID), **ip))

        Ad.objects.filter(pk=only.pk).update(targeting=dict(only.targeting, politics='exclude'))
        self.reload()
        self.assertIsNone(self.served('CHANNEL_MESSAGE', self.in_channel(DARYO_ID), **ip))
        self.assertEqual(self.served('CHANNEL_MESSAGE', self.in_channel(GAZETA_ID), user_id=2, **ip), str(only.pk))
        self.assertIsNone(self.served('CHANNEL_MESSAGE', self.in_channel(OTHER_ID), user_id=3, **ip))  # без тем — нет
        self.assertIsNone(self.served('CHANNEL_MESSAGE', self.in_channel(-1001000000099), user_id=3, **ip))

        video = self.users_ad(Placement.USERS_VIDEO_BANNER, politics='only')
        self.reload()
        self.assertEqual(self.served('VIDEO', {'channel_id': DARYO_ID, 'msg_id': 1}, user_id=4, **ip), str(video.pk))
        self.assertIsNone(self.served('VIDEO', {'channel_id': GAZETA_ID, 'msg_id': 1}, user_id=4, **ip))

    def test_own_channel_and_own_bot(self):
        in_channel = self.users_ad()  # продвигает kunuz
        in_bot = self.users_ad(Placement.USERS_BOT_BANNER, url='https://t.me/weatherbot')
        self.reload()
        ip = {'ip': IP_TASHKENT}
        self.assertIsNone(self.served('CHANNEL_MESSAGE', self.in_channel(KUNUZ_ID), **ip))
        self.assertEqual(self.served('CHANNEL_MESSAGE', self.in_channel(DARYO_ID), **ip), str(in_channel.pk))
        self.assertIsNone(self.served('BOT', {'bot_username': 'weatherbot'}, **ip))
        self.assertEqual(self.served('BOT', {'bot_username': 'kinobot'}, **ip), str(in_bot.pk))

    def test_normalization_helpers(self):
        known = {'en', 'uz', 'pt-br', 'zh-hans'}
        cases = {'en-US': 'en', 'UZ': 'uz', 'pt_BR': 'pt-br', 'zh-Hans': 'zh-hans', 'xx-YY': None, '': None, None: None}
        for value, expected in cases.items():
            self.assertEqual(audience.normalize_language(value, known), expected, value)
        for value, expected in {'iPad': 'ios', 'ANDROID': 'android', 'macos': 'desktop', 'tv': None, 5: None}.items():
            self.assertEqual(audience.normalize_device(value), expected, value)
        world = snapshot.load_world()
        index = world.locations
        self.assertEqual(index.resolve('UZ', ("Kattaqo'rg'on",), ()), {location("Kattaqo'rg'on Shahri"),
                                                                       location('Samarqand Region')})
        self.assertEqual(index.resolve('UZ', ('Qarshi',), ()), {location('Qarshi'), location('Qashqadaryo')})
        self.assertEqual(index.resolve('UZ', ('Sergeli',), ()), {location('Sergeli'), location('Tashkent')})
        self.assertEqual(index.resolve('UZ', (), ('Toshkent viloyati',)), {location('Tashkent Region')})
        self.assertEqual(index.resolve('KZ', ('Angren',), ()), frozenset())


class UsersSlotTests(UsersBase):
    """Общий аукцион слотов, VIDEO, заработок по USERS_CHANNEL_MESSAGE, ноль SQL"""

    def test_channel_slot_shared_auction(self):
        self.channel(DARYO_ID, language='uz')
        channels = self.channel_ad(cpm=1000, languages=['uz'])
        users = self.users_ad(cpm=2000)
        self.reload()
        where, ip = self.in_channel(DARYO_ID), {'ip': IP_TASHKENT}
        self.assertEqual(self.served('CHANNEL_MESSAGE', where, user_id=1, **ip), str(users.pk))  # Users дороже
        token = self.serve_slot('CHANNEL_MESSAGE', where, user_id=1, **ip).json()['ads'][0]['random_id']
        self.assertEqual(tokens.decode(token).placement, Placement.USERS_CHANNEL_MESSAGE)  # в токене — формат
        self.assertEqual(self.sent(1, self.view(token)), (1, 0))
        self.assertEqual(self.served('CHANNEL_MESSAGE', where, user_id=1, **ip), str(channels.pk))  # следующий
        Ad.objects.filter(pk=channels.pk).update(cpm=3000)
        self.reload()
        self.assertEqual(self.served('CHANNEL_MESSAGE', where, user_id=2, **ip), str(channels.pk))

    def test_bot_slot_shared_auction(self):
        bots = self.bot_ad(bots=['weatherbot'], cpm=3000)
        users = self.users_ad(Placement.USERS_BOT_BANNER, cpm=2000)
        self.reload()
        bot, ip = {'bot_username': 'weatherbot'}, {'ip': IP_TASHKENT}
        self.assertEqual(self.served('BOT', bot, user_id=1, **ip), str(bots.pk))
        self.assertEqual(self.served('BOT', {'bot_username': 'kinobot'}, user_id=1, **ip), str(users.pk))
        Ad.objects.filter(pk=users.pk).update(cpm=4000)
        self.reload()
        self.assertEqual(self.served('BOT', bot, user_id=1, **ip), str(users.pk))

    def bot_ad(self, bots=(), cpm=1000):
        response = create_ad(self.owner_client, Placement.BOT, cpm=cpm, targeting={'bots': list(bots)})
        self.assertEqual(response.status_code, 201, response.data)
        with self.captureOnCommitCallbacks(execute=True):
            self.mod_client.post(f'/api/moderation/ads/{response.data["id"]}/approve/')
        return Ad.objects.get(pk=response.data['id'])

    def test_video_slot(self):
        self.channel(DARYO_ID, language='uz')
        self.channel_ad(cpm=5000, languages=['uz'])  # сообщения в канале в видео не идут
        video = self.users_ad(Placement.USERS_VIDEO_BANNER)
        self.reload()
        context, ip = {'channel_id': DARYO_ID, 'msg_id': 42}, {'ip': IP_TASHKENT}
        data = self.serve_slot('VIDEO', context, **ip).json()
        self.assertEqual((data['start_delay'], data['between_delay'], data['ttl_seconds']),
                         (5, 120, settings.ADS_CACHE_TTL))
        item = data['ads'][0]
        self.assertEqual(tokens.decode(item['random_id']).ad_id, str(video.pk))
        self.assertNotIn('media', item)
        self.assertNotIn('button_type', item)
        self.assertEqual((item['min_display_duration'], item['max_display_duration'], item['text']),
                         (9, 30, video.text))

        pricing = AdPricing.load()
        pricing.video_start_delay, pricing.video_max_display = 3, 45
        pricing.save()
        self.reload()
        data = self.serve_slot('VIDEO', context, user_id=2, **ip).json()
        self.assertEqual((data['start_delay'], data['ads'][0]['max_display_duration']), (3, 45))
        empty = self.serve_slot('VIDEO', {'channel_id': OTHER_ID, 'msg_id': 1}).json()  # страны нет — Users нет
        self.assertEqual((empty['ads'], empty['start_delay']), ([], 3))
        for bad in ({'channel_id': DARYO_ID}, {'channel_id': DARYO_ID, 'msg_id': 0},
                    {'channel_id': DARYO_ID, 'msg_id': '5'}, {'msg_id': 5}):
            self.assertEqual(self.serve_slot('VIDEO', bad).status_code, 400, bad)

    def test_pricing_rejects_min_above_max(self):
        from django.core.exceptions import ValidationError
        pricing = AdPricing.load()
        pricing.video_min_display, pricing.video_max_display = 40, 30
        with self.assertRaises(ValidationError):
            pricing.full_clean()

    def test_partner_earns_on_users_channel_message_only(self):
        self.channel(DARYO_ID, language='uz', confirmed=True)
        message = self.users_ad(cpm=1000)
        video = self.users_ad(Placement.USERS_VIDEO_BANNER, cpm=1000)
        self.reload()
        ip = {'ip': IP_TASHKENT}
        token = self.serve_slot('CHANNEL_MESSAGE', self.in_channel(DARYO_ID), **ip).json()['ads'][0]['random_id']
        self.assertEqual(self.sent(1, self.view(token)), (1, 0))
        token = self.serve_slot('VIDEO', {'channel_id': DARYO_ID, 'msg_id': 1}, **ip).json()['ads'][0]['random_id']
        self.assertEqual(tokens.decode(token).channel_id, DARYO_ID)
        self.assertEqual(self.sent(1, self.view(token)), (1, 0))
        self.flush()
        daryo = ChannelEarning.objects.get(channel_id=DARYO_ID)
        self.assertEqual((daryo.total_earned, daryo.total_impressions), (Decimal('0.5'), 1))  # только сообщение
        self.assertEqual(EarningTransaction.objects.get(channel=daryo).ad_id, message.pk)
        self.assertEqual(self.fresh(video).spent, Decimal('1'))  # видео оплачено, но без партнёрки

    def test_zero_sql(self):
        self.channel(DARYO_ID, language='uz', topics=[SPORTS])
        self.users_ad(topics=[SPORTS], languages=['uz'])
        self.users_ad(Placement.USERS_VIDEO_BANNER)
        self.reload()
        with self.assertNumQueries(0):
            response = self.serve_slot('CHANNEL_MESSAGE', self.in_channel(DARYO_ID), ip=IP_ANGREN, lang='uz')
            token = response.json()['ads'][0]['random_id']
            self.assertEqual(self.sent(1, self.view(token), self.click(token)), (2, 0))
            data = self.serve_slot('VIDEO', {'channel_id': DARYO_ID, 'msg_id': 3}, ip=IP_ANGREN).json()
            self.assertEqual(len(data['ads']), 1)
