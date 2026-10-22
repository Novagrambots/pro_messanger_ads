"""
Тесты похожих каналов и ботов (этап 5б). AdGram замокан: норма, 404, 429, 503, таймаут, 401.
"""
import time
from datetime import timedelta
from unittest.mock import patch

import requests
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.ads.models import Placement
from apps.ads.tests import client_for, create_ad, ensure_test_peers, make_local_user
from apps.billing import adgram_client
from apps.billing.adgram_client import AdGramRejected, AdGramUnavailable, TgPeer
from apps.directories.models import TelegramPeer
from apps.directories.similar import COOLDOWN_KEY
from apps.directories.tasks import refresh_peers
from apps.directories.tests import CHANNEL_PAGE, JPEG, FakeResponse, SlowTelegram, TempMediaMixin, fake

ADGRAM = {'ADGRAM_INTERNAL_URL': 'http://adgram.test', 'ADGRAM_INTERNAL_KEY': 'test-key'}
PeerType = TelegramPeer.PeerType
PHOTO = 'https://cdn4.telesco.pe/file/similar.jpg'


def peer(ref, username, title='', photo=None, subscribers=None, monthly_users=None):
    return TgPeer(ads_ref=ref, username=username, title=title or username, photo_url=photo,
                  subscribers=subscribers, monthly_users=monthly_users)


class AdGramResponse:
    """Ответ requests.request для клиента AdGram"""

    def __init__(self, status=200, data=None, headers=None):
        self.status_code, self._data, self.headers = status, data, headers or {}

    def json(self):
        if self._data is None:
            raise ValueError
        return self._data


@override_settings(**ADGRAM)
class ClientTests(TestCase):
    """Клиент AdGram: статусы ответов → наши исключения"""

    def call(self, response, func=adgram_client.search_channel, arg='kunuz'):
        with patch('apps.billing.adgram_client.requests.request', side_effect=[response]) as request:
            result = func(arg)
        return result, request

    def test_ok_and_404(self):
        result, request = self.call(AdGramResponse(data={
            'id': 'h:abc', 'username': 'kunuz', 'title': 'Kun.uz', 'photoUrl': PHOTO}))
        self.assertEqual(result, peer('h:abc', 'kunuz', 'Kun.uz', PHOTO))
        args, kwargs = request.call_args
        self.assertEqual((args[0], args[1]), ('POST', 'http://adgram.test/api/internal/novagram/tg/search-channel'))
        self.assertEqual((kwargs['json'], kwargs['timeout']), ({'username': 'kunuz'}, (2, 10)))  # свой таймаут
        self.assertEqual(kwargs['headers'], {'X-Internal-Key': 'test-key'})
        self.assertIsNone(self.call(AdGramResponse(404, {'code': 'NOT_FOUND'}))[0])

        result, request = self.call(AdGramResponse(data={'bots': [
            {'id': 'h:1', 'username': 'abot', 'title': 'A', 'photoUrl': None, 'monthlyUsers': 1200}]}),
            adgram_client.similar_bots, ['h:x'])
        self.assertEqual(result, [peer('h:1', 'abot', 'A', monthly_users=1200)])
        self.assertEqual(request.call_args.kwargs['json'], {'botIds': ['h:x']})

    def test_errors(self):
        with self.assertRaises(AdGramUnavailable) as exc:
            self.call(AdGramResponse(429, {'code': 'TG_RATE_LIMIT'}, {'Retry-After': '30'}))
        self.assertEqual(exc.exception.retry_after, 30)
        with self.assertRaises(AdGramUnavailable):
            self.call(AdGramResponse(503, {'code': 'TG_UNAVAILABLE'}))
        with self.assertRaises(AdGramUnavailable):
            self.call(requests.Timeout('read timeout'))
        with self.assertRaises(AdGramRejected):
            self.call(AdGramResponse(401, {'code': 'UNAUTHORIZED'}))
        with self.assertRaises(AdGramUnavailable):  # 200, но без списка
            self.call(AdGramResponse(data={}), adgram_client.similar_channels, ['h:x'])


@override_settings(**ADGRAM)
class SimilarTests(TempMediaMixin, TestCase):
    def setUp(self):
        super().setUp()
        ensure_test_peers()  # kunuz, daryo (каналы) и weatherbot (бот) — «свежие», с числовыми id
        TelegramPeer.objects.filter(username='daryo').update(ads_ref='h:daryo')
        # Уже известный (свежий, с числовым id) канал, у которого в AdGram сменился ads_ref
        TelegramPeer.objects.create(username='uznews', peer_type=PeerType.CHANNEL, telegram_id=-1001000000003,
                                    title='uznews', ads_ref='h:old', fetched_at=timezone.now())
        self.client = client_for(make_local_user())
        self.search = patch('apps.billing.adgram_client.search_channel',
                            side_effect=lambda name, timeout: peer(f'h:{name}', name))
        self.similar = patch('apps.billing.adgram_client.similar_channels', return_value=[
            peer('h:fresh', 'FreshNews', 'Fresh News', PHOTO, subscribers=12000),
            peer('h:kunuz', 'kunuz'),               # входной канал — не возвращаем
            peer('h:private', None, 'Без имени'),    # без username — в таргетинг не добавить
            peer('h:new', 'UzNews', 'UzNews', subscribers=500000),     # известный, но ads_ref другой
        ])

    def get(self, value, path='similar-channels', param='channels'):
        return self.client.get(f'/api/ads/{path}/', {param: value})

    def test_similar_channels(self):
        with self.search as search, self.similar as similar, fake({'https://cdn4.telesco.pe/': FakeResponse(JPEG)}):
            response = self.get('kunuz,daryo')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([item['username'] for item in response.data], ['freshnews', 'uznews'])
        fresh = response.data[0]
        self.assertEqual((fresh['title'], fresh['subscribers']), ('Fresh News', 12000))
        self.assertTrue(fresh['avatar_url'].startswith('http://testserver/media/tg_avatars/freshnews'))

        # search — только для канала без ads_ref; похожие — по ads_ref, а не по числовым id
        self.assertEqual([call.args[0] for call in search.call_args_list], ['kunuz'])
        self.assertEqual(similar.call_args.args[0], ['h:kunuz', 'h:daryo'])

        kunuz = TelegramPeer.objects.get(username='kunuz')
        self.assertEqual((kunuz.ads_ref, kunuz.telegram_id), ('h:kunuz', -1001000000001))  # id не тронут
        uznews = TelegramPeer.objects.get(username='uznews')
        self.assertEqual((uznews.ads_ref, uznews.title, uznews.subscribers), ('h:new', 'UzNews', 500000))
        self.assertEqual(uznews.telegram_id, -1001000000003)
        self.assertTrue(uznews.is_fresh())  # данные резолвера (тип, id, свежесть) не тронуты

        new = TelegramPeer.objects.get(username='freshnews')
        self.assertEqual((new.peer_type, new.ads_ref, new.telegram_id), (PeerType.CHANNEL, 'h:fresh', None))
        self.assertFalse(new.is_fresh())  # из AdGram — «протухший»: резолвер проверит сам

    def test_cache_7_days_and_ads_ref_reuse(self):
        with self.search, self.similar, fake({'https://cdn4.telesco.pe/': FakeResponse(JPEG)}):
            self.get('kunuz,daryo')
        with self.search as search, self.similar as similar:
            response = self.get('daryo, @KunUz')  # тот же набор, другой порядок — из кэша
            self.assertEqual([item['username'] for item in response.data], ['freshnews', 'uznews'])
            self.assertEqual((search.call_count, similar.call_count), (0, 0))

            self.get('kunuz')  # другой набор — в AdGram, но ads_ref kunuz уже известен
            self.assertEqual((search.call_count, similar.call_count), (0, 1))

        with patch.object(cache, 'set', wraps=cache.set) as cache_set, self.search, self.similar:
            self.get('daryo')
        ttl = [call.args[2] for call in cache_set.call_args_list if call.args[0].startswith('similar:channels:')]
        self.assertEqual(ttl, [7 * 24 * 60 * 60])

    def test_nothing_found_on_ads_telegram(self):
        with patch('apps.billing.adgram_client.search_channel', return_value=None), self.similar as similar:
            TelegramPeer.objects.update(ads_ref=None)
            response = self.get('kunuz')
        self.assertEqual(response.data, [])
        similar.assert_not_called()

    def test_unavailable(self):
        # 401 — ошибка настройки: пользователю тот же 503, в лог — error
        with self.assertLogs('apps.directories.similar', level='ERROR') as logs:
            for error in (AdGramUnavailable('503'), AdGramUnavailable('timeout'), AdGramRejected(401, {})):
                cache.clear()
                with self.subTest(error=error), \
                        patch('apps.billing.adgram_client.similar_channels', side_effect=error):
                    response = self.get('daryo')
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.data['error'], 'Похожие сейчас недоступны')
        self.assertEqual(len(logs.output), 1)

        with override_settings(ADGRAM_INTERNAL_URL=''), self.similar as similar:
            self.assertEqual(self.get('daryo').status_code, 503)
        similar.assert_not_called()

    def test_429_cooldown(self):
        with patch('apps.billing.adgram_client.similar_channels',
                   side_effect=AdGramUnavailable('429', retry_after=3600)), \
                self.assertLogs('apps.directories.similar', 'WARNING') as logs:
            self.assertEqual(self.get('daryo').status_code, 503)
        self.assertTrue(cache.get(COOLDOWN_KEY))
        self.assertIn('similar —, аватары 0/0 за — → 503 (AdGram не ответил: 429)', logs.output[0])
        with self.search as search, self.similar as similar:
            self.assertEqual(self.get('kunuz').status_code, 503)  # остываем — в AdGram не ходим
        search.assert_not_called()
        similar.assert_not_called()

    def test_avatars_in_parallel_with_limit(self):
        # 6 аватаров разом; один «завис» — ждём не дольше AVATAR_SECONDS и отдаём без него
        photos = [f'https://cdn4.telesco.pe/file/p{i}.jpg' for i in range(6)]
        results = [peer(f'h:s{i}', f'simchan{i}', photo=photos[i]) for i in range(6)]
        tg = SlowTelegram(delay=0.2, hang=[photos[5]])
        self.addCleanup(tg.release.set)
        with self.search, patch('apps.billing.adgram_client.similar_channels', return_value=results), \
                patch('apps.directories.telegram.requests.get', side_effect=tg), \
                patch('apps.directories.similar.AVATAR_SECONDS', 1), \
                self.assertLogs('apps.directories.similar', 'INFO') as logs:
            started = time.monotonic()
            response = self.get('kunuz')
            took = time.monotonic() - started
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(tg.max_active, 6)  # все разом, а не по одному
        self.assertLess(took, 2.5)  # лимит 1 с, а не 5 с «зависшего»
        with_avatar = [item['username'] for item in response.data if item['avatar_url']]
        self.assertEqual(with_avatar, [f'simchan{i}' for i in range(5)])
        self.assertFalse(TelegramPeer.objects.get(username='simchan5').avatar)
        # Время этапов — одной строкой
        self.assertEqual(len(logs.output), 1)
        self.assertRegex(logs.output[0], r'INFO:apps\.directories\.similar:Похожие channels: имён 1, '
                                         r'search \d+\.\d с \(запросов 1\), similar \d+\.\d с, '
                                         r'аватары 5/6 за \d+\.\d с → найдено 6$')

    def test_new_peers_checked_in_background(self):
        cdn = fake({'https://cdn4.telesco.pe/': FakeResponse(JPEG)})
        with self.search, self.similar, cdn, patch('apps.directories.similar.refresh_peers.delay') as delay, \
                self.captureOnCommitCallbacks(execute=True):
            self.get('kunuz,daryo')
        delay.assert_called_once_with(['freshnews'])  # только новая запись: uznews и kunuz уже были

        # Из кэша — в AdGram не ходим, новых записей нет, задачу не ставим
        with self.search, self.similar, patch('apps.directories.similar.refresh_peers.delay') as delay, \
                self.captureOnCommitCallbacks(execute=True):
            self.get('kunuz,daryo')
        delay.assert_not_called()

        # Брокер недоступен — ответ всё равно 200, в лог — warning
        cache.clear()
        TelegramPeer.objects.filter(username='freshnews').delete()
        with self.search, self.similar, cdn, \
                patch('apps.directories.similar.refresh_peers.delay', side_effect=OSError('broker')), \
                self.assertLogs('apps.directories.similar', 'WARNING') as logs, \
                self.captureOnCommitCallbacks(execute=True):
            response = self.get('kunuz,daryo')
        self.assertEqual(response.status_code, 200)
        self.assertIn('OSError', logs.output[-1])

    def test_input_validation(self):
        for value in ('', 'kun', ','.join(f'channel{i}' for i in range(11)), 'https://evil.example/x'):
            with self.subTest(value=value):
                self.assertEqual(self.get(value).status_code, 400)
        self.assertEqual(self.get('kunuz', 'similar-bots', 'bots').status_code, 400)  # не бот
        many_bots = ','.join(f'name{i}bot' for i in range(10))
        self.assertEqual(self.get(many_bots, 'similar-bots', 'bots').status_code, 400)

    def test_similar_bots(self):
        with patch('apps.billing.adgram_client.search_bot', return_value=peer('h:weather', 'weatherbot')), \
                patch('apps.billing.adgram_client.similar_bots',
                      return_value=[peer('h:rain', 'RainBot', 'Rain', monthly_users=4321)]):
            response = self.get('weatherbot', 'similar-bots', 'bots')
        self.assertEqual(response.data, [{'username': 'rainbot', 'title': 'Rain', 'avatar_url': None,
                                          'monthly_users': 4321}])
        self.assertEqual(TelegramPeer.objects.get(username='rainbot').peer_type, PeerType.BOT)

    def test_similar_bots_for_bot_without_bot_suffix(self):
        # @BotFather — бот по данным резолвера: похожие работают, хоть имя и не кончается на «bot»
        with patch('apps.billing.adgram_client.search_bot', return_value=peer('h:father', 'BotFather')) as search, \
                patch('apps.billing.adgram_client.similar_bots',
                      return_value=[peer('h:rain', 'RainBot', 'Rain', monthly_users=4321)]):
            response = self.get('@BotFather', 'similar-bots', 'bots')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([item['username'] for item in response.data], ['rainbot'])
        self.assertEqual(search.call_args.args[0], 'botfather')

        # Резолвер имени не знает — правило про суффикс, как раньше
        response = self.get('somechannel', 'similar-bots', 'bots')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['error'], '@somechannel — не бот: имя бота кончается на «bot»')

    def test_throttle_20_per_minute(self):
        with self.search, self.similar, fake({'https://cdn4.telesco.pe/': FakeResponse(JPEG)}):
            codes = [self.get('daryo').status_code for _ in range(21)]
        self.assertEqual((codes[:20], codes[20]), ([200] * 20, 429))


@override_settings(TELEGRAM_RESOLVER_BOT_TOKEN='42:TOKEN')
class SimilarThenTargetingTests(TempMediaMixin, TestCase):
    """Канал из «похожих» при добавлении в таргетинг проверяется резолвером и получает числовой id"""

    def setUp(self):
        super().setUp()
        self.client = client_for(make_local_user())

    def test_resolver_checks_channel_from_adgram(self):
        ensure_test_peers()
        TelegramPeer.objects.create(username='freshnews', peer_type=PeerType.CHANNEL, ads_ref='h:fresh',
                                    fetched_at=timezone.now() - TelegramPeer.FRESH_FOR - timedelta(minutes=1))
        routes = {
            'https://t.me/freshnews': FakeResponse(CHANNEL_PAGE.replace('@kunuz', '@freshnews')),
            'https://api.telegram.org/bot42:TOKEN/getChat': FakeResponse(json_data={'ok': True, 'result': {
                'id': -1009876543210, 'type': 'channel', 'title': 'Fresh News'}}),
            'https://cdn4.telesco.pe/': FakeResponse(JPEG),
        }
        with fake(routes):
            response = create_ad(self.client, Placement.CHANNEL_MESSAGE, targeting={'channels': ['freshnews']})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['targeting']['channels'], [{'username': 'freshnews', 'id': -1009876543210}])
        self.assertEqual(TelegramPeer.objects.get(username='freshnews').ads_ref, 'h:fresh')  # хэш остался

    @override_settings(ADGRAM_INTERNAL_URL='', ADGRAM_INTERNAL_KEY='')
    def test_ad_creation_does_not_need_adgram(self):
        with patch('apps.billing.adgram_client.requests.request', side_effect=AssertionError('AdGram')), \
                fake({}):
            response = create_ad(self.client, Placement.CHANNEL_MESSAGE, targeting={'channels': ['daryo']})
        self.assertEqual(response.status_code, 201, response.data)


@override_settings(TELEGRAM_RESOLVER_BOT_TOKEN='42:TOKEN')
class RefreshPeersTests(TempMediaMixin, TestCase):
    """Фоновая проверка новых записей из «похожих»: тип, числовой id и аватар — до сохранения объявления"""

    def test_checks_only_stale(self):
        ensure_test_peers()  # kunuz свежий — в Telegram за ним не ходим (иначе FakeTelegram уронит тест)
        stale = timezone.now() - TelegramPeer.FRESH_FOR - timedelta(minutes=1)
        TelegramPeer.objects.create(username='freshnews', peer_type=PeerType.CHANNEL, ads_ref='h:fresh',
                                    fetched_at=stale)
        routes = {
            'https://t.me/freshnews': FakeResponse(CHANNEL_PAGE.replace('@kunuz', '@freshnews')),
            'https://api.telegram.org/bot42:TOKEN/getChat': FakeResponse(json_data={'ok': True, 'result': {
                'id': -1009876543210, 'type': 'channel', 'title': 'Fresh News'}}),
            'https://cdn4.telesco.pe/': FakeResponse(JPEG),
        }
        with fake(routes):
            refresh_peers(['freshnews', 'kunuz'])
        fresh = TelegramPeer.objects.get(username='freshnews')
        self.assertTrue(fresh.is_fresh())
        self.assertEqual((fresh.telegram_id, fresh.ads_ref), (-1009876543210, 'h:fresh'))
        self.assertTrue(fresh.avatar)

        # Telegram не ответил — задача не падает, запись остаётся непроверенной (проверим при сохранении)
        TelegramPeer.objects.filter(username='freshnews').update(fetched_at=stale)
        with fake({'https://t.me/freshnews': requests.Timeout('t')}):
            refresh_peers(['freshnews'])
        self.assertFalse(TelegramPeer.objects.get(username='freshnews').is_fresh())
