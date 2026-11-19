"""
Мост старых версий приложения (apps/ads/serving/legacy.py): старые URL выдачи — поиск, чаты, клики —
показывают объявления v2 с общими бюджетом, частотой, статистикой и заработком партнёра; старые заказы —
в том же аукционе по цене; лимиты запросов v2; без старых заказов — ноль SQL; выключатель и падение Redis.
Нужен Redis (как в test_serving).
"""
import json
from decimal import Decimal
from types import SimpleNamespace

from django.test import RequestFactory, SimpleTestCase, override_settings
from django.utils import timezone

from apps.ads.models import Ad, Placement
from apps.ads.serving import legacy, snapshot, store as S
from apps.ads.test_serving import (API_KEY, GAZETA_ID, QUERY, REDIS_DOWN, ChannelServingBase,  # noqa: F401
                                   ServingBase, setUpModule, tearDownModule)
from apps.ads.tests import DARYO_ID, KUNUZ_ID, client_for, create_ad, make_local_user
from apps.chat_ads.models import ChatAdOrder
from apps.directories.models import TelegramPeer
from apps.partner.models import ChannelEarning, EarningTransaction
from apps.search_ads.models import Order


class LegacyMixin:
    """Запросы старого приложения и старые заказы"""

    def old(self, url, body):
        return self.client.post(url, json.dumps(body), content_type='application/json', HTTP_X_API_KEY=API_KEY)

    def old_search(self, viewer_id='1', tag=QUERY):
        return self.old('/api/search_ads/order/search/', {'tag': tag, 'viewer_id': viewer_id})

    def old_chat(self, viewer_id='1', channel_name='daryo', channel_id=DARYO_ID):
        body = {'channel_name': channel_name, 'viewer_id': viewer_id}
        if channel_id is not None:
            body['channel_id'] = channel_id
        return self.old('/api/chat_ads/order/search/', body)

    def old_click(self, order_id, viewer_id='1', kind='search_ads'):
        return self.old(f'/api/{kind}/order/click/', {'order_id': str(order_id), 'viewer_id': viewer_id})

    def search_order(self, spm, tag=QUERY, budget='100.00', max_views_per_user=1, channel='old_chan'):
        """Старый заказ поиска: создан через старый API (деньги списаны) и одобрен"""
        response = self.owner_client.post('/api/search_ads/order/create/', {
            'channel_id': channel, 'channel_name': channel, 'order_name': 'Старый заказ', 'tags': [tag],
            'spm': spm, 'budget': budget, 'max_views_per_user': max_views_per_user,
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        order = Order.objects.get(channel_name=channel)
        order.approve(self.moderator)
        return order

    def chat_order(self, spm, channels='daryo', budget='100.00'):
        name = f'Старый чат-заказ {ChatAdOrder.objects.count() + 1}'
        response = self.owner_client.post('/api/chat_ads/order/create/', {
            'order_name': name, 'text': 'Старый текст', 'link': 'https://example.com',
            'channels': channels, 'spm': spm, 'budget': budget,
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        order = ChatAdOrder.objects.get(order_name=name)
        order.approve(self.moderator)
        return order


class LegacySearchTests(LegacyMixin, ServingBase):
    """Старый поиск: объявление v2 по тегу, общие частота и деньги, аукцион со старыми заказами"""

    def test_v2_ad_on_old_search(self):
        ad = self.active_ad()
        self.reload()
        response = self.old_search('1')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {'channel_id': str(KUNUZ_ID), 'channel_name': 'kunuz',
                                           'order_id': str(ad.pk), 'platform': 'TELEGRAM'})
        # Выдача = показ: тот же сброс в Postgres, что и у новых версий
        self.flush()
        ad = self.fresh(ad)
        self.assertEqual((ad.views_total, ad.spent), (1, Decimal('1')))
        self.assertEqual(self.stat(ad).views, 1)

    def test_superadmin_cheap_ad_money(self):
        """CPM 1 (можно только суперадмину): показ стоит 0,001 сума — Redis и сброс считают точно"""
        boss = make_local_user('boss_cheap')
        boss.is_superuser = True
        boss.save(update_fields=['is_superuser'])
        response = create_ad(client_for(boss), Placement.SEARCH, cpm=1, targeting={'queries': [QUERY]})
        self.assertEqual(response.status_code, 201, response.data)
        ad = Ad.objects.get(pk=response.data['id'])
        self.reload()
        self.assertEqual(self.old_search('1').json()['order_id'], str(ad.pk))
        self.assertEqual(self.serve_ad(2)['placement'], Placement.SEARCH)  # и новой версии
        self.flush()
        self.assertEqual((self.fresh(ad).views_total, self.fresh(ad).spent), (1, Decimal('0.001')))

    def test_user_account_ad_not_served_to_old_versions(self):
        """Аккаунт пользователя старое приложение не покажет, а показ списывается при выдаче — старым не отдаём"""
        account = self.active_ad()
        peer = TelegramPeer.objects.create(username='shop_admin', peer_type=TelegramPeer.PeerType.USER,
                                           title='Admin', fetched_at=timezone.now())
        Ad.objects.filter(pk=account.pk).update(promoted_peer=peer, url='https://t.me/shop_admin')
        self.reload()
        self.assertEqual(self.old_search('1').status_code, 404)  # «рекламы нет», а не чужой формат
        self.flush()
        self.assertEqual((self.fresh(account).views_total, self.fresh(account).spent), (0, Decimal('0')))
        self.assertEqual(self.serve_ad(1)['promoted']['type'], 'USER')  # новые версии — как раньше

        channel = self.active_ad()
        self.reload()
        response = self.old_search('2')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['order_id'], str(channel.pk))

    def test_tag_is_normalized_like_search_phrase(self):
        self.active_ad(queries=['oʻzbek tili'])
        self.reload()
        self.assertEqual(self.old_search('1', tag="  O'zbek   Tili ").status_code, 200)

    def test_frequency_shared_with_new_versions(self):
        ad = self.active_ad()  # views_per_user_per_day = 1
        self.reload()
        self.assertEqual(self.old_search('1').status_code, 200)
        self.assertEqual(self.old_search('1').status_code, 404)  # сегодня уже показали
        self.assertIsNone(self.serve_ad(1))                      # и новая версия того же зрителя не покажет
        self.assertEqual(self.old_search('2').status_code, 200)  # другой зритель
        self.flush()
        self.assertEqual(self.fresh(ad).views_total, 2)

    def test_non_numeric_viewer_gets_no_v2_ads(self):
        ad = self.active_ad()
        self.reload()
        self.assertEqual(self.old_search('viewer_1').status_code, 404)
        self.flush()
        self.assertEqual(self.fresh(ad).views_total, 0)

    def test_auction_with_legacy_orders(self):
        ad = self.active_ad(cpm=1000)
        order = self.search_order(spm='5000.00')  # дороже объявления v2
        self.reload()
        response = self.old_search('1')
        self.assertEqual(response.json()['order_id'], str(order.order_id))
        # Этому зрителю старый заказ больше нельзя (лимит 1) — следующим идёт объявление v2
        self.assertEqual(self.old_search('1').json()['order_id'], str(ad.pk))
        order.refresh_from_db()
        self.assertEqual(order.shown_views, 1)

    def test_v2_wins_over_cheaper_legacy_order(self):
        ad = self.active_ad(cpm=1000)
        order = self.search_order(spm='5.00')
        self.reload()
        self.assertEqual(self.old_search('1').json()['order_id'], str(ad.pk))
        order.refresh_from_db()
        self.assertEqual(order.shown_views, 0)
        # v2 этому зрителю сегодня уже нельзя — старый заказ всё ещё крутится
        self.assertEqual(self.old_search('1').json()['order_id'], str(order.order_id))

    def test_click_counts_only_after_serve(self):
        ad = self.active_ad()
        self.reload()
        self.assertEqual(self.old_click(ad.pk, '1').status_code, 400)  # не выдавали — старый путь: заказа нет
        self.assertEqual(self.old_search('1').status_code, 200)
        self.assertEqual(self.old_click(ad.pk, '1').status_code, 200)
        self.assertEqual(self.old_click(ad.pk, '1').status_code, 200)  # повтор по той же выдаче не считается
        self.assertEqual(self.old_click(ad.pk, '2').status_code, 400)  # зрителю 2 не выдавали
        self.flush()
        self.assertEqual(self.fresh(ad).clicks_total, 1)

    def test_click_with_padded_ids(self):
        """Выдача обрезает пробелы в viewer_id — клик тоже, иначе зрители выдачи и клика разойдутся"""
        ad = self.active_ad()
        self.reload()
        self.assertEqual(self.old_search(' 1 ').status_code, 200)
        self.assertEqual(self.old_click(f' {ad.pk} ', ' 1 ').status_code, 200)
        self.flush()
        self.assertEqual(self.fresh(ad).clicks_total, 1)

    def test_legacy_order_click_still_works(self):
        order = self.search_order(spm='5.00')
        self.reload()
        self.assertEqual(self.old_click(order.order_id, '1').status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.clicks, 1)

    def test_rate_limit_answers_no_ad(self):
        ad = self.active_ad()
        Ad.objects.filter(pk=ad.pk).update(views_per_user_per_day=0)
        order = self.search_order(spm='5.00', max_views_per_user=10)
        self.reload()
        with override_settings(ADS_RATE_VIEWER_SERVE=2):
            self.assertEqual([self.old_search('1').status_code for _ in range(3)], [200, 200, 404])
            self.assertEqual(self.old_search('2').status_code, 200)  # у другого зрителя свой счётчик
            # Нечисловой id (только старые заказы) — тоже под лимитом: бюджет скриптом не выжечь
            self.assertEqual([self.old_search('abc').status_code for _ in range(3)], [200, 200, 404])
        order.refresh_from_db()
        self.assertEqual(order.shown_views, 2)

    def test_no_sql_without_legacy_orders(self):
        self.active_ad()
        self.reload()
        with self.assertNumQueries(0):
            self.assertEqual(self.old_search('1').status_code, 200)
            self.assertEqual(self.old_search('2', tag='нет такого').status_code, 404)

    def test_kill_switch(self):
        ad = self.active_ad()
        order = self.search_order(spm='5.00')
        self.reload()
        with override_settings(ADS_LEGACY_BRIDGE=False):
            self.assertEqual(self.old_search('1').json()['order_id'], str(order.order_id))
            self.assertEqual(self.old_search('2').json()['order_id'], str(order.order_id))
        self.flush()
        self.assertEqual(self.fresh(ad).views_total, 0)  # мост выключен — объявление v2 не показывалось
        self.assertEqual(self.r.keys(S.PREFIX + 'lg:*'), [])

    def test_redis_down_legacy_orders_still_served(self):
        self.active_ad()
        order = self.search_order(spm='5.00')
        legacy._last_outage_log = 0
        with override_settings(ADS_REDIS_URL=REDIS_DOWN), self.assertLogs('apps.ads.serving.legacy', 'ERROR'):
            self.assertEqual(self.old_search('1').json()['order_id'], str(order.order_id))

    def test_index_follows_legacy_orders(self):
        self.reload()
        self.assertEqual(snapshot.current().legacy.search_tags, frozenset())
        with self.captureOnCommitCallbacks(execute=True):
            order = self.search_order(spm='5.00')  # одобрение → версия снимка
        snapshot.refresh_if_needed()
        self.assertEqual(snapshot.current().legacy.search_tags, frozenset([QUERY]))
        # Клик сохраняет только clicks — снимок не перечитывается
        version = self.r.get(S.VERSION)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.old_click(order.order_id, '1').status_code, 200)
        self.assertEqual(self.r.get(S.VERSION), version)


class LegacyChatTests(LegacyMixin, ChannelServingBase):
    """Старая выдача в чатах: сообщение в канале v2 по channel_id, заработок партнёра, клики"""

    def test_v2_channel_ad_on_old_chat(self):
        ad = self.channel_ad(channels=['daryo'])
        self.channel(DARYO_ID, confirmed=True)  # партнёр, доля 0.5
        self.reload()
        response = self.old_chat('1')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {
            'order_id': str(ad.pk), 'order_name': 'kunuz', 'text': ad.text, 'link': 'https://t.me/kunuz',
            'platform': 'TELEGRAM', 'media_url': None, 'media_type': None,
            'total_views': 9999, 'clicks': 0, 'shown_views': 0, 'remaining_views': 9999,
        })
        self.assertEqual(self.old_click(ad.pk, '1', kind='chat_ads').status_code, 200)
        self.flush()
        ad = self.fresh(ad)
        self.assertEqual((ad.views_total, ad.clicks_total, ad.spent), (1, 1, Decimal('1')))
        daryo = ChannelEarning.objects.get(channel_id=DARYO_ID)
        self.assertEqual((daryo.total_earned, daryo.total_impressions), (Decimal('0.5'), 1))
        self.assertTrue(EarningTransaction.objects.filter(channel=daryo, ad=ad).exists())

    def test_without_channel_id_only_legacy_orders(self):
        self.channel_ad(channels=['daryo'])
        self.reload()
        self.assertEqual(self.old_chat('1', channel_id=None).status_code, 404)
        self.assertEqual(self.old_chat('1', channel_id=123).status_code, 404)  # не формат -100…
        order = self.chat_order(spm='5.00', channels='')  # старый заказ во всех каналах
        self.reload()
        self.assertEqual(self.old_chat('2', channel_id=None).json()['order_id'], str(order.order_id))

    def test_chat_auction_with_legacy_order(self):
        ad = self.channel_ad(channels=['daryo'])
        order = self.chat_order(spm='5000.00')
        self.reload()
        self.assertEqual(self.old_chat('1').json()['order_id'], str(order.order_id))
        order.refresh_from_db()
        self.assertEqual(order.shown_views, 1)
        cheap = self.chat_order(spm='5.00', channels='gazeta')
        self.reload()
        self.assertEqual(self.old_chat('2').json()['order_id'], str(order.order_id))  # 5000 > cpm 1000
        Ad.objects.filter(pk=ad.pk).update(cpm=6_000_000)
        self.reload()
        self.assertEqual(self.old_chat('3').json()['order_id'], str(ad.pk))
        cheap.refresh_from_db()
        self.assertEqual(cheap.shown_views, 0)  # другой канал


class LegacyResultTests(SimpleTestCase):
    def test_chat_result_media(self):
        entry = SimpleNamespace(id='ad-1', payload={
            'text': 'Текст', 'url': 'https://t.me/kunuz',
            'promoted': {'username': 'kunuz', 'title': 'Kun.uz', 'telegram_id': KUNUZ_ID},
            'media': {'type': 'PHOTO', 'url': '/media/ads/x.jpg'},
        })
        request = RequestFactory().post('/api/chat_ads/order/search/')
        data = legacy.chat_result(request, legacy.Shown(entry, 5))
        self.assertEqual((data['order_name'], data['media_type'], data['media_url'], data['remaining_views']),
                         ('Kun.uz', 'IMAGE', 'http://testserver/media/ads/x.jpg', 5))
