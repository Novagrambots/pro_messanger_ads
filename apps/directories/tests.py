"""
Тесты справочников и данных по t.me-ссылке. Вся сеть замокана: страницы t.me собраны
по реальной разметке (канал, бот, группа, пользователь, «не найден»), Bot API — по документации.
"""
import shutil
import tempfile
import threading
from datetime import timedelta
from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.ads.models import Ad, Placement
from apps.ads.tests import KUNUZ_ID, SPORTS, client_for, create_ad, ensure_test_peers, make_local_user
from apps.ads.validators import ERR_TELEGRAM_DOWN, not_checked_in_time_error
from apps.directories import telegram
from apps.directories.models import Country, Language, Location, TelegramPeer, Topic

User = get_user_model()
PeerType = TelegramPeer.PeerType
TOKEN = '123456:SECRET-TOKEN'
JPEG = b'\xff\xd8\xff\xe0' + b'0' * 100


def tme_page(kind, title='Kun.uz | Rasmiy kanal', extras=(), button='View in Telegram',
             image='https://cdn4.telesco.pe/file/avatar.jpg', preview_channel=False):
    """Страница t.me в той же разметке, что у настоящей (kind: View / Launch / Contact)"""
    extra_html = ''.join(f'<div class="tgme_page_extra">{extra}</div>\n' for extra in extras)
    preview = ('<div class="tgme_page_context_link_wrap"><a class="tgme_page_context_link" '
               'href="/s/kunuz">Preview channel</a></div>') if preview_channel else ''
    return f'''<html><head><title>Telegram: {kind} @kunuz</title>
<meta property="og:title" content="{title}">
<meta property="og:image" content="{image}"></head><body>
<div class="tgme_page">
<div class="tgme_page_title" dir="auto">
  <span dir="auto">{title}</span><i class="verified-icon"> ✔</i>
</div>
{extra_html}<div class="tgme_page_action">
  <a class="tgme_action_button_new shine" href="tg://resolve?domain=kunuz">{button}</a>
</div>
{preview}</div></body></html>'''


CHANNEL_PAGE = tme_page('View', extras=['70 529 subscribers'], preview_channel=True)
BOT_PAGE = tme_page('Launch', title='Weather Bot', extras=['\n  @WeatherBot\n', '1 234 monthly users'],
                    button='Start Bot')
GROUP_PAGE = tme_page('View', title='TDLib chat', extras=['9 362 members, 456 online'],
                      image='data:image/svg+xml;base64,AAAA')
USER_PAGE = tme_page('Contact', title='Aliaksei Levin', extras=['\n  @levlam\n'], button='Send Message',
                     image='https://telegram.org/img/t_logo_2x.png')
NOT_FOUND_PAGE = '''<html><head><title>Telegram: Contact @zzqq</title>
<meta property="og:title" content="Telegram: Contact @zzqq">
<meta property="og:image" content="https://telegram.org/img/t_logo_2x.png"></head><body>
<div class="tgme_page"><div class="tgme_page_icon"><i class="tgme_icon_user"></i></div>
<div class="tgme_page_description">If you have <strong>Telegram</strong>, you can contact
<a class="tgme_username_link" href="tg://resolve?domain=zzqq">@zzqq</a> right away.</div>
<div class="tgme_page_action"><a class="tgme_action_button_new" href="tg://resolve?domain=zzqq">Send Message</a>
</div></div></body></html>'''


class FakeResponse:
    def __init__(self, body=b'', status=200, json_data=None, headers=None):
        self.body = body.encode() if isinstance(body, str) else body
        self.status_code = status
        self._json = json_data
        self.headers = headers or {}

    def iter_content(self, size):
        for i in range(0, len(self.body), size):
            yield self.body[i:i + size]

    def json(self):
        if self._json is None:
            raise ValueError('не JSON')
        return self._json

    def close(self):
        pass


class FakeTelegram:
    """Подменяет requests.get: ответ по началу URL. Неизвестный URL — ошибка теста"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        assert kwargs.get('allow_redirects') is False, 'редиректы должны быть выключены'
        for prefix, answer in self.routes.items():
            if url.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError(f'Неожиданный запрос: {url}')


def fake(routes):
    return patch('apps.directories.telegram.requests.get', side_effect=FakeTelegram(routes))


class SlowTelegram:
    """
    requests.get для проверки параллельности: любое t.me/<имя> — страница канала, остальное — JPEG,
    каждый ответ — через delay с. Имена и URL из hang ждут release (не успевают в deadline).
    Считает, сколько запросов шло одновременно (max_active)
    """

    def __init__(self, delay=0.2, hang=()):
        self.delay, self.hang = delay, set(hang)
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.active = self.max_active = 0
        self.urls = []

    def __call__(self, url, **kwargs):
        with self.lock:
            self.urls.append(url)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            name = url.removeprefix('https://t.me/')
            self.release.wait(5 if name in self.hang or url in self.hang else self.delay)
            if url.startswith('https://t.me/'):
                return FakeResponse(CHANNEL_PAGE.replace('@kunuz', f'@{name}'))
            return FakeResponse(JPEG)
        finally:
            with self.lock:
                self.active -= 1

    def fetched(self):
        return sorted(url.removeprefix('https://t.me/') for url in self.urls if url.startswith('https://t.me/'))


class TempMediaMixin:
    """Аватары в тестах — во временную папку, а не в media/ проекта"""

    def setUp(self):
        super().setUp()
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=media)
        override.enable()
        self.addCleanup(override.disable)
        cache.clear()
        self.addCleanup(cache.clear)


class ParsePageTests(TestCase):
    """Признаки типа на странице t.me"""

    def test_types(self):
        channel = telegram.parse_page(CHANNEL_PAGE)
        self.assertEqual((channel['peer_type'], channel['title'], channel['subscribers']),
                         (PeerType.CHANNEL, 'Kun.uz | Rasmiy kanal', 70529))
        self.assertEqual(channel['avatar_url'], 'https://cdn4.telesco.pe/file/avatar.jpg')

        bot = telegram.parse_page(BOT_PAGE)
        self.assertEqual((bot['peer_type'], bot['monthly_users'], bot['subscribers']), (PeerType.BOT, 1234, None))

        group = telegram.parse_page(GROUP_PAGE)
        self.assertEqual((group['peer_type'], group['subscribers']), (PeerType.GROUP, 9362))
        self.assertIsNone(group['avatar_url'])  # data:-заглушка не качается

        user = telegram.parse_page(USER_PAGE)
        self.assertEqual(user['peer_type'], PeerType.USER)
        self.assertIsNone(user['avatar_url'])  # логотип telegram.org — не аватар

        self.assertTrue(telegram.parse_page(NOT_FOUND_PAGE)['not_found'])

    def test_foreign_avatar_host_ignored(self):
        for image in ('https://evil.example/file/a.jpg', 'http://cdn4.telesco.pe/file/a.jpg',
                      'https://cdn4.telesco.pe.evil.example/a.jpg'):
            page = tme_page('View', extras=['5 subscribers'], image=image)
            self.assertIsNone(telegram.parse_page(page)['avatar_url'], image)

    def test_username_rules(self):
        for good, expected in (('t.me/KunUz', 'kunuz'), ('https://t.me/kunuz/', 'kunuz'), ('@kunuz', 'kunuz')):
            self.assertEqual(telegram.normalize_username(good), expected)
        for bad in ('kun', 'a' * 33, 'kun uz', '1kunuz', 'https://evil.com/kunuz', '../etc', None):
            with self.assertRaises(ValueError):
                telegram.normalize_username(bad)


class ResolveTests(TempMediaMixin, TestCase):
    def test_channel_from_page_and_cache(self):
        routes = {'https://t.me/kunuz': FakeResponse(CHANNEL_PAGE),
                  'https://cdn4.telesco.pe/': FakeResponse(JPEG)}
        with override_settings(TELEGRAM_RESOLVER_BOT_TOKEN=''), fake(routes) as get:
            peer = telegram.resolve('kunuz')
        self.assertEqual((peer.peer_type, peer.subscribers, peer.telegram_id), (PeerType.CHANNEL, 70529, None))
        self.assertTrue(peer.avatar.name.startswith('tg_avatars/kunuz'))
        self.assertEqual(get.call_count, 2)

        with fake({}) as get:  # свежие данные — без сети
            self.assertEqual(telegram.resolve('kunuz').pk, peer.pk)
        get.assert_not_called()

    def test_channel_with_bot_api(self):
        routes = {
            'https://t.me/kunuz': FakeResponse(CHANNEL_PAGE),
            f'https://api.telegram.org/bot{TOKEN}/getChat': FakeResponse(json_data={'ok': True, 'result': {
                'id': -1001234567890, 'type': 'channel', 'title': 'Kun.uz',
                'photo': {'small_file_id': 'AQAD', 'big_file_id': 'AQAE'}}}),
            f'https://api.telegram.org/bot{TOKEN}/getFile': FakeResponse(json_data={'ok': True, 'result': {
                'file_id': 'AQAD', 'file_path': 'profile_photos/file_1.jpg'}}),
            f'https://api.telegram.org/file/bot{TOKEN}/profile_photos/file_1.jpg': FakeResponse(JPEG),
        }
        with override_settings(TELEGRAM_RESOLVER_BOT_TOKEN=TOKEN), fake(routes) as get:
            peer = telegram.resolve('kunuz')
        self.assertEqual((peer.telegram_id, peer.title, peer.peer_type), (-1001234567890, 'Kun.uz', PeerType.CHANNEL))
        self.assertTrue(peer.avatar)
        self.assertNotIn('cdn4.telesco.pe', [url for url, _ in get.side_effect.calls])  # фото взяли из Bot API

    def test_bot_api_error_is_not_logged_with_token(self):
        routes = {'https://t.me/kunuz': FakeResponse(CHANNEL_PAGE),
                  f'https://api.telegram.org/bot{TOKEN}/getChat': requests.ConnectionError(
                      f'Max retries exceeded with url: /bot{TOKEN}/getChat'),
                  'https://cdn4.telesco.pe/': FakeResponse(JPEG)}
        with override_settings(TELEGRAM_RESOLVER_BOT_TOKEN=TOKEN), fake(routes), \
                self.assertLogs('apps.directories.telegram', level='WARNING') as logs:
            peer = telegram.resolve('kunuz')
        self.assertEqual(peer.peer_type, PeerType.CHANNEL)  # данные со страницы сохранились
        self.assertIsNone(peer.telegram_id)
        self.assertNotIn(TOKEN, '\n'.join(logs.output))
        self.assertNotIn('SECRET', '\n'.join(logs.output))

    def test_bot_is_not_asked_in_bot_api(self):
        # getChat ботов не находит — не зовём (в маршрутах Bot API нет: вызов уронил бы тест)
        routes = {'https://t.me/weatherbot': FakeResponse(BOT_PAGE), 'https://cdn4.telesco.pe/': FakeResponse(JPEG)}
        with override_settings(TELEGRAM_RESOLVER_BOT_TOKEN=TOKEN), fake(routes) as get:
            peer = telegram.resolve('weatherbot')
        self.assertEqual((peer.peer_type, peer.monthly_users), (PeerType.BOT, 1234))
        self.assertFalse(any('api.telegram.org' in url for url, _ in get.side_effect.calls))

    def test_not_found_cached_for_an_hour(self):
        with fake({'https://t.me/zzqqzz': FakeResponse(NOT_FOUND_PAGE)}):
            peer = telegram.resolve('zzqqzz')
        self.assertTrue(peer.not_found)
        with fake({}) as get:
            telegram.resolve('zzqqzz')
        get.assert_not_called()

        TelegramPeer.objects.filter(pk=peer.pk).update(fetched_at=timezone.now() - timedelta(minutes=61))
        with fake({'https://t.me/zzqqzz': FakeResponse(NOT_FOUND_PAGE)}) as get:
            telegram.resolve('zzqqzz')
        self.assertEqual(get.call_count, 1)

    def test_timeout(self):
        with fake({'https://t.me/kunuz': requests.Timeout('timeout')}):
            with self.assertRaises(telegram.TelegramUnavailable):
                telegram.resolve('kunuz')
        self.assertFalse(TelegramPeer.objects.exists())

        # Есть протухшие данные — отдаём их, а не ошибку
        stale = TelegramPeer.objects.create(username='kunuz', peer_type=PeerType.CHANNEL,
                                            fetched_at=timezone.now() - timedelta(days=2))
        with fake({'https://t.me/kunuz': requests.Timeout('timeout')}):
            self.assertEqual(telegram.resolve('kunuz').pk, stale.pk)

    def test_redirect_is_not_followed(self):
        with fake({'https://t.me/kunuz': FakeResponse(status=302, headers={'Location': 'http://10.0.0.1/'})}):
            with self.assertRaises(telegram.TelegramUnavailable):
                telegram.resolve('kunuz')

    def test_big_or_not_image_avatar_skipped(self):
        for avatar in (FakeResponse(JPEG + b'0' * (1024 * 1024)),                       # больше 1 МБ
                       FakeResponse(JPEG, headers={'Content-Length': str(5 * 1024 * 1024)}),
                       FakeResponse(b'<html>not an image</html>')):
            TelegramPeer.objects.all().delete()
            with fake({'https://t.me/kunuz': FakeResponse(CHANNEL_PAGE), 'https://cdn4.telesco.pe/': avatar}):
                peer = telegram.resolve('kunuz')
            self.assertEqual(peer.peer_type, PeerType.CHANNEL)  # остальное сохранилось
            self.assertFalse(peer.avatar)

    def test_deadline(self):
        with self.assertRaises(telegram.DeadlineExceeded), fake({}):
            telegram.PeerBatch(seconds=0).resolve(['newchannel'])


class ResolveApiTests(TempMediaMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client = client_for(make_local_user())

    def get(self, url):
        return self.client.get('/api/ads/resolve/', {'url': url})

    def test_ok_not_found_and_errors(self):
        with fake({'https://t.me/kunuz': FakeResponse(CHANNEL_PAGE),
                   'https://cdn4.telesco.pe/': FakeResponse(JPEG)}):
            response = self.get('t.me/KunUz')
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.data['username'], response.data['peer_type'], response.data['subscribers']),
                         ('kunuz', 'CHANNEL', 70529))
        self.assertTrue(response.data['avatar_url'].startswith('http://testserver/media/tg_avatars/kunuz'))

        with fake({'https://t.me/zzqqzz': FakeResponse(NOT_FOUND_PAGE)}):
            self.assertEqual(self.get('@zzqqzz').status_code, 404)
        with fake({'https://t.me/daryo': requests.Timeout('t')}):
            self.assertEqual(self.get('daryo').status_code, 503)
        with fake({}) as get:
            self.assertEqual(self.get('https://evil.example/kunuz').status_code, 400)
            self.assertEqual(self.get('t.me/ab').status_code, 400)
        get.assert_not_called()
        self.assertEqual(APIClient().get('/api/ads/resolve/', {'url': 'kunuz'}).status_code, 401)

    def test_throttle_60_per_minute(self):
        ensure_test_peers()
        codes = [self.get('kunuz').status_code for _ in range(61)]
        self.assertEqual(codes[:60], [200] * 60)
        self.assertEqual(codes[60], 429)


class DirectoriesApiTests(TempMediaMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client = client_for(make_local_user())

    def test_lists(self):
        languages = self.client.get('/api/ads/directories/languages/').data
        self.assertEqual(len(languages), 71)
        self.assertIn({'code': 'zh-hans', 'name_en': 'Chinese (Simplified)', 'name_ru': 'Китайский (упрощённый)',
                       'name_uz': 'Xitoy tili (soddalashtirilgan)'}, languages)
        topics = self.client.get('/api/ads/directories/topics/').data
        self.assertEqual((len(topics), topics[0]['id']), (41, 1))
        self.assertEqual(len(self.client.get('/api/ads/directories/countries/').data), 250)

        locations = self.client.get('/api/ads/directories/locations/', {'country': 'uz', 'q': 'tash'}).data
        self.assertEqual({item['name_en'] for item in locations}, {'Tashkent', 'Tashkent Region'})
        self.assertEqual(locations[0]['country'], 'UZ')
        by_russian = self.client.get('/api/ads/directories/locations/', {'q': 'Самарк'}).data
        self.assertEqual({item['name_en'] for item in by_russian}, {'Samarkand', 'Samarqand Region'})
        self.assertEqual(APIClient().get('/api/ads/directories/languages/').status_code, 401)

    def test_cache_reset_on_admin_edit(self):
        codes = lambda: {item['code'] for item in self.client.get('/api/ads/directories/languages/').data}  # noqa
        self.assertIn('uz', codes())  # теперь в кэше

        language = Language.objects.get(code='uz')
        language.is_active = False
        language.save()  # так сохраняет админка — сигнал сбрасывает кэш
        self.assertNotIn('uz', codes())

        boss = User.objects.create_superuser('boss', 'boss@test.uz', 'pass12345')
        admin = APIClient()
        admin.force_login(boss)
        admin.post('/admin/directories/language/uz/change/', {
            'code': 'uz', 'name_en': 'Uzbek', 'name_ru': 'Узбекский', 'name_uz': 'Oʻzbek tili', 'is_active': 'on'})
        self.assertIn('uz', codes())


class TargetingWithDirectoriesTests(TempMediaMixin, TestCase):
    """Таргетинг объявления: справочники вместо свободных строк, каналы и боты — через Telegram"""

    def setUp(self):
        super().setUp()
        self.client = client_for(make_local_user())
        no_network = patch('apps.directories.telegram.requests.get', side_effect=AssertionError('сеть'))
        no_network.start()
        self.addCleanup(no_network.stop)

    def invalid(self, placement, targeting, key='targeting', **overrides):
        response = create_ad(self.client, placement, targeting=targeting, **overrides)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn(key, response.data)
        return response.data[key]

    def test_dictionary_values(self):
        users = Placement.USERS_CHANNEL_MESSAGE
        self.assertIn('xx', str(self.invalid(users, {'countries': ['UZ'], 'languages': ['xx']})))
        self.assertIn('QQ', str(self.invalid(users, {'countries': ['QQ']})))
        self.assertIn('999', str(self.invalid(users, {'countries': ['UZ'], 'topics': [999]})))
        self.assertIn("'45'", str(self.invalid(users, {'countries': ['UZ'], 'topics': ['45']})))  # строка — нельзя
        Language.objects.filter(code='ru').update(is_active=False)
        cache.clear()
        self.assertIn('ru', str(self.invalid(users, {'countries': ['UZ'], 'languages': ['ru']})))

        tashkent = Location.objects.get(name_en='Tashkent').pk
        self.assertIn(str(tashkent), str(self.invalid(users, {'countries': ['KZ'], 'locations': [tashkent]})))
        self.assertIn('123456', str(self.invalid(users, {'countries': ['UZ'], 'locations': [123456]})))
        response = create_ad(self.client, users, targeting={
            'countries': ['UZ'], 'locations': [tashkent], 'languages': ['zh-hans'], 'topics': [SPORTS],
            'exclude_topics': [7]})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['targeting']['topics'], [SPORTS])

    def test_peer_types(self):
        TelegramPeer.objects.create(username='newsbot', peer_type=PeerType.CHANNEL, fetched_at=timezone.now())
        TelegramPeer.objects.create(username='uzchat', peer_type=PeerType.GROUP, fetched_at=timezone.now())
        TelegramPeer.objects.create(username='someone', peer_type=PeerType.USER, fetched_at=timezone.now())
        TelegramPeer.objects.create(username='gone', not_found=True, fetched_at=timezone.now())

        self.assertIn('не канал', str(self.invalid(Placement.CHANNEL_MESSAGE, {'channels': ['weatherbot']})))
        self.assertIn('не бот', str(self.invalid(Placement.BOT, {'bots': ['newsbot']})))
        self.assertIn('не найден', str(self.invalid(Placement.CHANNEL_MESSAGE, {'channels': ['gone']})))
        self.assertIn('не канал', str(self.invalid(Placement.USERS_CHANNEL_MESSAGE,
                                                    {'countries': ['UZ'], 'channel_audiences': ['uzchat']})))
        for url in ('t.me/uzchat', 't.me/someone', 't.me/gone'):
            self.invalid(Placement.BOT, TARGETING_BOT, key='url', url=url)

        # Продвигается бот — себя исключать не из чего
        response = create_ad(self.client, Placement.CHANNEL_MESSAGE, url='t.me/weatherbot',
                             targeting={'channels': ['kunuz']})
        self.assertEqual(response.data['targeting']['exclude_channels'], [])
        self.assertEqual(response.data['targeting']['channels'], [{'username': 'kunuz', 'id': KUNUZ_ID}])
        self.assertEqual(response.data['promoted_peer']['peer_type'], 'BOT')
        self.assertEqual(Ad.objects.get(pk=response.data['id']).promoted_peer.username, 'weatherbot')

    def test_new_channel_is_resolved_during_creation(self):
        page = CHANNEL_PAGE.replace('@kunuz', '@freshnews')
        routes = {'https://t.me/freshnews': FakeResponse(page), 'https://cdn4.telesco.pe/': FakeResponse(JPEG)}
        with fake(routes):
            response = create_ad(self.client, Placement.CHANNEL_MESSAGE, targeting={'channels': ['@FreshNews']})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['targeting']['channels'], [{'username': 'freshnews', 'id': None}])

        with fake({'https://t.me/slownews': requests.Timeout('t')}):
            errors = self.invalid(Placement.CHANNEL_MESSAGE, {'channels': ['slownews']})
        self.assertIn(ERR_TELEGRAM_DOWN, str(errors))

    def stale_peer(self, username, peer_type=PeerType.CHANNEL, telegram_id=None, not_found=False):
        return TelegramPeer.objects.create(username=username, peer_type=peer_type, telegram_id=telegram_id,
                                           not_found=not_found,
                                           fetched_at=timezone.now() - TelegramPeer.FRESH_FOR - timedelta(hours=1))

    def slow(self, tg):
        self.addCleanup(tg.release.set)  # отпускаем «зависшие» потоки после теста
        return patch('apps.directories.telegram.requests.get', side_effect=tg)

    def test_many_new_channels_checked_in_parallel(self):
        # 40 каналов из «похожих» (протухшие, без id) — лимита больше нет, проверяем разом
        names = [f'similarchan{i}' for i in range(40)]
        for name in names:
            self.stale_peer(name)
        tg = SlowTelegram(delay=0.1)
        with self.slow(tg):
            response = create_ad(self.client, Placement.CHANNEL_MESSAGE, targeting={'channels': names})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(tg.fetched(), sorted(names))
        self.assertEqual(tg.max_active, telegram.WORKERS)  # по 8 одновременно, а не по одному
        self.assertTrue(all(peer.is_fresh() for peer in TelegramPeer.objects.filter(username__in=names)))

    def test_checked_stale_peers_taken_as_is(self):
        # Протухшие, но уже проверенные: каналы с числовым id и бот — без похода в Telegram (сеть запрещена)
        names = [f'oldchan{i}' for i in range(30)]
        for i, name in enumerate(names):
            self.stale_peer(name, telegram_id=-1002000000000 - i)
        self.stale_peer('oldbot', PeerType.BOT)
        response = create_ad(self.client, Placement.CHANNEL_MESSAGE, url='t.me/oldchan0',
                             targeting={'channels': names[1:]})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['targeting']['channels'][0], {'username': 'oldchan1', 'id': -1002000000001})
        self.assertEqual(response.data['targeting']['exclude_channels'],
                         [{'username': 'oldchan0', 'id': -1002000000000}])  # себя исключили по старому id
        response = create_ad(self.client, Placement.BOT, targeting={'bots': ['oldbot']})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertFalse(TelegramPeer.objects.get(username='oldbot').is_fresh())  # не трогали

    def test_stale_group_user_not_found_and_channel_without_id_rechecked(self):
        self.stale_peer('uzchat', PeerType.GROUP)
        self.stale_peer('someone', PeerType.USER)
        self.stale_peer('gone', PeerType.UNKNOWN, not_found=True)
        self.stale_peer('fromsimilar')
        names = ['uzchat', 'someone', 'gone', 'fromsimilar']
        tg = SlowTelegram(delay=0)  # теперь все четверо — каналы
        with self.slow(tg):
            response = create_ad(self.client, Placement.CHANNEL_MESSAGE, targeting={'channels': names})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(tg.fetched(), sorted(names))
        self.assertEqual(set(TelegramPeer.objects.filter(username__in=names).values_list('peer_type', flat=True)),
                         {PeerType.CHANNEL})

    def test_not_checked_in_time(self):
        fast, slow = [f'fastchan{i}' for i in range(5)], ['slowchan0', 'slowchan1', 'slowchan2']
        tg = SlowTelegram(delay=0, hang=slow)
        with self.slow(tg), patch('apps.ads.serializers.PeerBatch', lambda: telegram.PeerBatch(1)):
            response = create_ad(self.client, Placement.CHANNEL_MESSAGE, targeting={'channels': fast + slow})
        self.assertEqual(response.status_code, 400, response.data)
        # Общая ошибка таргетинга (не поля channels): фронт показывает её под блоком таргетинга
        self.assertEqual(response.data['targeting'], [not_checked_in_time_error(3)])
        self.assertEqual(response.data['targeting'][0],
                         'Не успели проверить в Telegram 3 канала — нажмите «Сохранить» ещё раз')
        self.assertTrue(all(peer.is_fresh() for peer in TelegramPeer.objects.filter(username__in=fast)))
        self.assertFalse(TelegramPeer.objects.filter(username__in=slow).exists())

        # Со второй попытки проверяем только тех, кого не успели
        tg.release.set()
        again = SlowTelegram(delay=0)
        with self.slow(again):
            response = create_ad(self.client, Placement.CHANNEL_MESSAGE, targeting={'channels': fast + slow})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(again.fetched(), slow)

    def test_not_checked_in_time_error_text(self):
        for count, word in ((1, 'канал'), (2, 'канала'), (4, 'канала'), (5, 'каналов'), (11, 'каналов'),
                            (14, 'каналов'), (21, 'канал'), (22, 'канала'), (100, 'каналов')):
            self.assertIn(f' {count} {word} —', not_checked_in_time_error(count))


TARGETING_BOT = {'bots': ['t.me/weatherbot']}


class DataTests(TestCase):
    """Начальные данные справочников (data-миграция)"""

    def test_counts_and_samples(self):
        self.assertEqual((Language.objects.count(), Topic.objects.count(), Country.objects.count(),
                          Location.objects.count()), (71, 41, 250, 36))
        self.assertEqual(Topic.objects.get(pk=37).name_en, 'Politics & Incidents')
        self.assertTrue(Country.objects.filter(code='XK').exists())
        self.assertFalse(Country.objects.filter(code='FT').exists())
        nukus = Location.objects.get(name_en='Nukus')
        self.assertEqual((nukus.country_id, nukus.region_uz), ('UZ', 'Qoraqalpogʻiston'))
