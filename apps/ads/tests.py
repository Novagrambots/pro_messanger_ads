"""
Тесты объявлений v2: создание по каждому месту показа, таргетинг, минимальный CPM,
статусы и переходы, точные возвраты, пополнение, даты, доступ к чужим объявлениям.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.account.services import create_user_with_balance
from apps.ads import validators
from apps.ads.models import Ad, AdPricing, AdStateError, AdStatus, Placement
from apps.ads.validators import (ERR_CHANNELS_WITH_LANGUAGES, ERR_CHANNELS_WITH_TOPICS,
                                 ERR_TOPICS_WITHOUT_LANGUAGE)
from apps.billing.adgram_client import OperationResult
from apps.billing.models import Balance, MoneyOperation as Op
from apps.directories.models import TelegramPeer

User = get_user_model()

ADGRAM_SETTINGS = {'ADGRAM_INTERNAL_URL': 'http://adgram.test', 'ADGRAM_INTERNAL_KEY': 'test-key'}

# Минимально валидный таргетинг для каждого места показа
TARGETING = {
    Placement.SEARCH: {'queries': ['новости uz']},
    Placement.BOT: {'bots': ['t.me/weatherbot']},
    Placement.CHANNEL_MESSAGE: {'languages': ['uz']},
    Placement.USERS_CHANNEL_MESSAGE: {'countries': ['UZ']},
    Placement.USERS_VIDEO_BANNER: {'countries': ['UZ']},
    Placement.USERS_BOT_BANNER: {'countries': ['UZ']},
}


def make_local_user(username='adv', amount='1000000.00'):
    user = User.objects.create_user(username, password='pass12345')
    Balance.objects.create(user=user, amount=Decimal(amount))
    return user


def client_for(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


# Каналы и боты из тестов — сразу «свежие» в кэше TelegramPeer, чтобы тесты не ходили в Telegram
KUNUZ_ID, DARYO_ID = -1001000000001, -1001000000002
TEST_PEERS = {
    'kunuz': (TelegramPeer.PeerType.CHANNEL, KUNUZ_ID),
    'daryo': (TelegramPeer.PeerType.CHANNEL, DARYO_ID),
    'weatherbot': (TelegramPeer.PeerType.BOT, None),
    'botfather': (TelegramPeer.PeerType.BOT, None),  # бот без «bot» на конце имени
}
SPORTS = 45  # id темы «Sports» в справочнике (как у Telegram)


def ensure_test_peers():
    for username, (peer_type, telegram_id) in TEST_PEERS.items():
        TelegramPeer.objects.update_or_create(username=username, defaults={
            'peer_type': peer_type, 'telegram_id': telegram_id, 'title': username,
            'fetched_at': timezone.now(), 'not_found': False,
        })


_no_network = patch('apps.directories.telegram.requests.get',
                    side_effect=AssertionError('Тест полез в Telegram — добавьте имя в TEST_PEERS'))


def setUpModule():
    _no_network.start()  # страховка: любой поход в сеть из этих тестов — сразу ошибка


def tearDownModule():
    _no_network.stop()


def ad_payload(placement=Placement.BOT, **overrides):
    ensure_test_peers()
    payload = {
        'placement': placement, 'title': 'Тест', 'url': 'https://t.me/kunuz',
        'cpm': 1000, 'budget': '10000', 'agree_terms': True, 'targeting': TARGETING[placement],
    }
    if placement != Placement.SEARCH:
        payload['text'] = 'Свежие новости Узбекистана'
    payload.update(overrides)
    return payload


def create_ad(client, placement=Placement.BOT, **overrides):
    return client.post('/api/ads/', ad_payload(placement, **overrides), format='json')


def call_locked(ad, method, *args):
    """Переход так, как его делают вьюхи и админка: в транзакции, под select_for_update"""
    with transaction.atomic():
        locked = Ad.objects.select_for_update().get(pk=ad.pk)
        return getattr(locked, method)(*args)


def balance_of(user):
    return Balance.objects.get(user=user)


class AdCreateTests(TestCase):
    """Создание: каждое место показа, валидация, минимальный CPM, бюджет, согласие"""

    def setUp(self):
        self.user = make_local_user()
        self.client = client_for(self.user)

    def assert_invalid(self, response, key):
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn(key, response.data)

    def test_create_each_placement(self):
        expected_format = {Placement.SEARCH: 'search', Placement.BOT: 'bot', Placement.CHANNEL_MESSAGE: 'channels'}
        for placement in Placement:
            with self.subTest(placement=placement):
                response = create_ad(self.client, placement)
                self.assertEqual(response.status_code, 201, response.data)
                self.assertEqual(response.data['status'], AdStatus.IN_REVIEW)
                self.assertEqual(response.data['ad_format'], expected_format.get(placement, 'users'))
                self.assertEqual(response.data['budget'], '10000.00')
                self.assertEqual(response.data['remaining_budget'], '10000.00')
                self.assertEqual(response.data['terms_version'], '1.0')

                ad = Ad.objects.get(pk=response.data['id'])
                self.assertIsNotNone(ad.terms_accepted_at)
                op = Op.objects.get(order_id=ad.pk)
                self.assertEqual((op.kind, op.reason, op.order_type), (Op.Kind.DEBIT, Op.Reason.CREATE, 'ads'))

        self.assertEqual(balance_of(self.user).amount, Decimal('940000.00'))  # 6 × 10 000

    def test_search_rules(self):
        for key, overrides in (
            ('text', {'text': 'текст'}),
            ('show_channel_picture', {'show_channel_picture': True}),
            ('targeting', {'targeting': {}}),                                    # нет фраз
            ('targeting', {'targeting': {'queries': ['abc']}}),                  # короче 4
            ('targeting', {'targeting': {'queries': ['x' * 33]}}),               # длиннее 32
            ('targeting', {'targeting': {'queries': [f'фраза {i}' for i in range(11)]}}),  # больше 10
        ):
            with self.subTest(case=overrides):
                self.assert_invalid(create_ad(self.client, Placement.SEARCH, **overrides), key)

        response = create_ad(self.client, Placement.SEARCH, targeting={'queries': ['News uz', 'news UZ']})
        self.assertEqual(response.data['targeting'], {'queries': ['News uz']})  # дубль без учёта регистра убран

    def test_text_and_url_rules(self):
        for overrides, key in (
            ({'text': ''}, 'text'),
            ({'text': 'x' * 161}, 'text'),
            ({'text': 'строка\nвторая'}, 'text'),
            ({'url': 'https://example.com/kunuz'}, 'url'),
            ({'url': 't.me/ab'}, 'url'),
            ({'url': '@kunuz'}, 'url'),
        ):
            with self.subTest(overrides=overrides):
                self.assert_invalid(create_ad(self.client, **overrides), key)

        response = create_ad(self.client, url='https://t.me/KunUz/')
        self.assertEqual(response.data['url'], 'https://t.me/kunuz')

    def test_bot_targeting(self):
        self.assert_invalid(create_ad(self.client, targeting={'bots': ['t.me/kunuz']}), 'targeting')
        self.assert_invalid(create_ad(self.client, targeting={'bots': [f'bot{i}bot' for i in range(101)]}),
                            'targeting')

        response = create_ad(self.client, targeting={'bots': ['@WeatherBot', 'https://t.me/weatherbot']})
        self.assertEqual(response.data['targeting'], {'bots': ['weatherbot']})
        response = create_ad(self.client, targeting={})  # пусто = любые боты
        self.assertEqual(response.data['targeting'], {'bots': []})

    def test_bot_without_bot_suffix(self):
        # Резолвер уже знает, что @BotFather — бот: верим ему, а не имени
        response = create_ad(self.client, targeting={'bots': ['t.me/BotFather', 'weatherbot']})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['targeting'], {'bots': ['botfather', 'weatherbot']})

        # Данных резолвера нет — правило про суффикс (в Telegram не ходим: сеть в этих тестах запрещена)
        response = create_ad(self.client, targeting={'bots': ['somechannel']})
        self.assert_invalid(response, 'targeting')
        self.assertIn('@somechannel — не бот: имя бота кончается на «bot»', str(response.data))

        # Резолвер сказал «канал» — суффикс «bot» не спасает
        TelegramPeer.objects.create(username='newsbot', peer_type=TelegramPeer.PeerType.CHANNEL,
                                    fetched_at=timezone.now())
        response = create_ad(self.client, targeting={'bots': ['newsbot']})
        self.assert_invalid(response, 'targeting')
        self.assertIn('@newsbot — не бот', str(response.data))

    def test_users_targeting(self):
        for placement, targeting in (
            (Placement.USERS_CHANNEL_MESSAGE, {}),                                         # нет стран
            (Placement.USERS_BOT_BANNER, {'countries': ['UZ'], 'politics': 'only'}),      # нет политики
            (Placement.USERS_VIDEO_BANNER, {'countries': ['UZ'], 'politics': 'maybe'}),
            (Placement.USERS_VIDEO_BANNER, {'countries': ['UZ'], 'devices': ['ios', 'tv']}),
            (Placement.USERS_VIDEO_BANNER, {'countries': ['UZB']}),
            (Placement.USERS_CHANNEL_MESSAGE, {'countries': ['UZ'], 'queries': ['новости']}),
            (Placement.USERS_CHANNEL_MESSAGE,
             {'countries': ['UZ'], 'channel_audiences': [f'channel{i}' for i in range(101)]}),
        ):
            with self.subTest(placement=placement, targeting=targeting):
                self.assert_invalid(create_ad(self.client, placement, targeting=targeting), 'targeting')

        response = create_ad(self.client, Placement.USERS_CHANNEL_MESSAGE, targeting={
            'countries': ['uz'], 'devices': ['IOS', 'android'], 'politics': 'exclude',
            'languages': ['UZ', 'ru'], 'channel_audiences': ['@KunUz'],
        })
        self.assertEqual(response.status_code, 201, response.data)
        targeting = response.data['targeting']
        self.assertEqual(targeting['countries'], ['UZ'])
        self.assertEqual(targeting['devices'], ['ios', 'android'])
        self.assertEqual(targeting['languages'], ['uz', 'ru'])
        self.assertEqual(targeting['channel_audiences'], [{'username': 'kunuz', 'id': KUNUZ_ID}])
        self.assertEqual(targeting['politics'], 'exclude')

    def test_channel_message_rules(self):
        """Правила Telegram: либо языки + темы, либо явный список каналов"""
        placement = Placement.CHANNEL_MESSAGE
        for targeting, field, message in (
            ({'channels': ['daryo'], 'languages': ['uz']}, 'languages', ERR_CHANNELS_WITH_LANGUAGES),
            ({'channels': ['daryo'], 'topics': [SPORTS]}, 'topics', ERR_CHANNELS_WITH_TOPICS),
            ({'topics': [SPORTS]}, 'languages', ERR_TOPICS_WITHOUT_LANGUAGE),
        ):
            with self.subTest(targeting=targeting):
                response = create_ad(self.client, placement, targeting=targeting)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(str(response.data['targeting'][field]), message)

        self.assert_invalid(create_ad(self.client, placement, targeting={}), 'targeting')  # нигде не покажется
        self.assert_invalid(create_ad(self.client, placement, targeting={'languages': ['uz'], 'politics': 'only'}),
                            'targeting')  # у Каналов галочек про политику нет

        # Продвигаемый канал сам попадает в исключения
        by_channels = create_ad(self.client, placement, targeting={'channels': ['daryo', 'kunuz']})
        self.assertEqual(by_channels.status_code, 201, by_channels.data)
        self.assertEqual(by_channels.data['targeting']['exclude_channels'], [{'username': 'kunuz', 'id': KUNUZ_ID}])
        by_topics = create_ad(self.client, placement, targeting={'languages': ['uz'], 'topics': [SPORTS]})
        self.assertEqual(by_topics.status_code, 201, by_topics.data)
        self.assertEqual(by_topics.data['targeting']['exclude_channels'], [{'username': 'kunuz', 'id': KUNUZ_ID}])

    def test_min_cpm_with_and_without_markup(self):
        response = create_ad(self.client, cpm=999)
        self.assertEqual(response.data['cpm'][0], "CPM can't be less than 1000")
        self.assertEqual(create_ad(self.client, cpm=1000).status_code, 201)

        # Фото канала +30% к минимуму: 1000 × 1.3 = 1300
        response = create_ad(self.client, cpm=1299, show_channel_picture=True)
        self.assertEqual(response.data['cpm'][0], "CPM can't be less than 1300")
        self.assertEqual(create_ad(self.client, cpm=1300, show_channel_picture=True).status_code, 201)

        # Дробный минимум округляется вверх до сума: 333 × 1.3 = 432.9 → 433
        AdPricing.objects.filter(pk=1).update(min_cpm_bot=333)
        response = create_ad(self.client, cpm=432, show_channel_picture=True)
        self.assertEqual(response.data['cpm'][0], "CPM can't be less than 433")
        self.assertEqual(create_ad(self.client, cpm=433, show_channel_picture=True).status_code, 201)

    def test_budget_and_terms(self):
        self.assert_invalid(create_ad(self.client, budget='9999'), 'budget')         # меньше минимума
        self.assert_invalid(create_ad(self.client, budget='10000.555'), 'budget')    # больше 2 знаков
        self.assert_invalid(create_ad(self.client, agree_terms=False), 'agree_terms')
        payload = ad_payload()
        del payload['agree_terms']
        self.assert_invalid(self.client.post('/api/ads/', payload, format='json'), 'agree_terms')

        poor = make_local_user('poor', amount='5000.00')
        self.assert_invalid(create_ad(client_for(poor)), 'budget')                    # не хватает денег
        self.assertFalse(Ad.objects.filter(user=poor).exists())
        self.assertFalse(Op.objects.filter(user=poor).exists())

    def test_dates_on_create(self):
        now = timezone.now()
        start, end = (now + timedelta(days=1)).isoformat(), (now + timedelta(days=2)).isoformat()
        for overrides, key in (
            ({'start_date': start}, 'start_date'),                                       # старт только для ON_HOLD
            ({'initial_status': 'ON_HOLD', 'start_date': (now - timedelta(hours=1)).isoformat()}, 'start_date'),
            ({'initial_status': 'ON_HOLD', 'start_date': (now + timedelta(days=366)).isoformat()}, 'start_date'),
            ({'initial_status': 'ON_HOLD', 'start_date': end, 'end_date': start}, 'end_date'),
        ):
            with self.subTest(overrides=overrides):
                self.assert_invalid(create_ad(self.client, **overrides), key)

        response = create_ad(self.client, initial_status='ON_HOLD', start_date=start, end_date=end)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(create_ad(self.client, end_date=end).status_code, 201)  # конец — для любого статуса

    def test_schedule_on_create(self):
        weekdays_9_18 = [261632] * 5 + [0, 0]
        for placement, schedule in (
            (Placement.BOT, {'enabled': True, 'days': weekdays_9_18, 'timezone': 'viewer'}),
            (Placement.BOT, {'enabled': True, 'days': [0] * 7, 'timezone': 'Asia/Tashkent'}),
            (Placement.BOT, {'enabled': True, 'days': weekdays_9_18, 'timezone': 'Mars/Olympus'}),
            (Placement.BOT, {'enabled': True, 'days': [1] * 6}),
            (Placement.BOT, {'enabled': True, 'days': [1 << 24] * 7}),
            (Placement.BOT, {'enabled': True, 'days': weekdays_9_18, 'hours': 1}),
        ):
            with self.subTest(schedule=schedule):
                self.assert_invalid(create_ad(self.client, placement, schedule=schedule), 'schedule')

        response = create_ad(self.client, schedule={'enabled': True, 'days': weekdays_9_18,
                                                    'timezone': 'Europe/Moscow'})
        self.assertEqual(response.data['schedule']['days'], weekdays_9_18)
        response = create_ad(self.client, Placement.USERS_BOT_BANNER,
                             schedule={'enabled': True, 'days': weekdays_9_18, 'timezone': 'viewer'})
        self.assertEqual(response.status_code, 201, response.data)


class SuperadminLimitsTests(TestCase):
    """
    Суперадмин (свои кампании, перенос старых заказов поиска): без минимального CPM и лимита в 10 фраз.
    У рекламодателей правила те же
    """

    def setUp(self):
        self.boss = make_local_user('boss')
        self.boss.is_superuser = True
        self.boss.save(update_fields=['is_superuser'])
        self.boss_client = client_for(self.boss)
        self.user_client = client_for(make_local_user('adv'))

    def test_superadmin_any_cpm_and_many_queries(self):
        queries = [f'фраза {i}' for i in range(50)]
        response = create_ad(self.boss_client, Placement.SEARCH, cpm=1, targeting={'queries': queries})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual((response.data['cpm'], response.data['status']), (1, AdStatus.ACTIVE))
        self.assertEqual(response.data['targeting']['queries'], queries)
        # Наценки за медиа и фото канала — тоже не мешают
        response = create_ad(self.boss_client, Placement.BOT, cpm=1, show_channel_picture=True)
        self.assertEqual(response.status_code, 201, response.data)

    def test_superadmin_queries_cap(self):
        too_many = [f'фраза {i}' for i in range(validators.SUPERADMIN_MAX_QUERIES + 1)]
        response = create_ad(self.boss_client, Placement.SEARCH, targeting={'queries': too_many})
        self.assertEqual(response.status_code, 400)
        self.assertIn('targeting', response.data)
        # Длина фразы — те же 4–32 символа
        response = create_ad(self.boss_client, Placement.SEARCH, targeting={'queries': ['abc']})
        self.assertEqual(response.status_code, 400)

    def test_advertiser_limits_unchanged(self):
        response = create_ad(self.user_client, Placement.SEARCH, cpm=1)
        self.assertEqual(response.data['cpm'][0], "CPM can't be less than 1000")
        response = create_ad(self.user_client, Placement.SEARCH,
                             targeting={'queries': [f'фраза {i}' for i in range(11)]})
        self.assertEqual(response.status_code, 400)
        self.assertIn('targeting', response.data)

    def test_patch_cpm_below_minimum(self):
        boss_ad = create_ad(self.boss_client, Placement.SEARCH).data['id']
        response = self.boss_client.patch(f'/api/ads/{boss_ad}/', {'cpm': 5}, format='json')
        self.assertEqual((response.status_code, response.data['cpm']), (200, 5))

        user_ad = create_ad(self.user_client, Placement.SEARCH).data['id']
        response = self.user_client.patch(f'/api/ads/{user_ad}/', {'cpm': 5}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['cpm'][0], "CPM can't be less than 1000")

    def test_reprice_keeps_superadmin_cheap_cpm(self):
        """Смена наценки креатива: CPM суперадмина растёт по наценке, но до минимума не поднимается"""
        boss_ad = create_ad(self.boss_client, Placement.BOT, cpm=1).data['id']
        response = self.boss_client.patch(f'/api/ads/{boss_ad}/', {'show_channel_picture': True}, format='json')
        self.assertEqual((response.status_code, response.data['cpm']), (200, 2))  # 1 × 1,3 → вверх до 2
        response = self.boss_client.patch(f'/api/ads/{boss_ad}/', {'show_channel_picture': False}, format='json')
        self.assertEqual(response.data['cpm'], 1)  # 2 / 1,3 → вниз до 1


class UserAccountAdTests(TestCase):
    """
    Аккаунт пользователя (t.me/<user>, напр. «напиши админу») рекламирует любой рекламодатель: действие MESSAGE,
    кнопка USER. Android такую рекламу не рисует — таргетинг «только Android» не пропускаем
    """

    def setUp(self):
        self.client = client_for(make_local_user())
        TelegramPeer.objects.create(username='shop_admin', peer_type=TelegramPeer.PeerType.USER, title='Admin',
                                    fetched_at=timezone.now())

    def test_advertiser_can_promote_user_account(self):
        from apps.ads.serving.snapshot import make_entry
        for placement in Placement:
            with self.subTest(placement=placement):
                response = create_ad(self.client, placement, url='https://t.me/shop_admin')
                self.assertEqual(response.status_code, 201, response.data)
                self.assertEqual(response.data['action_type'], 'MESSAGE')
                self.assertEqual(response.data['promoted_peer']['peer_type'], 'USER')
                ad = Ad.objects.get(pk=response.data['id'])
                self.assertEqual(make_entry(ad).payload['button_type'], 'USER')

    def test_group_is_not_promotable(self):
        TelegramPeer.objects.create(username='uz_chat', peer_type=TelegramPeer.PeerType.GROUP,
                                    fetched_at=timezone.now())
        response = create_ad(self.client, url='https://t.me/uz_chat')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['url'][0], validators.ERR_NOT_PROMOTABLE.format('uz_chat'))

    def test_android_only_devices_allowed_for_user_account(self):
        """Android с 1.4.0 рисует аккаунт пользователя (старым версиям выдача его не отдаёт) — «только Android» можно"""
        for devices in ([], ['android'], ['ANDROID'], ['android', 'ios'], ['desktop']):
            with self.subTest(devices=devices):
                response = create_ad(self.client, Placement.USERS_BOT_BANNER, url='https://t.me/shop_admin',
                                     targeting={'countries': ['UZ'], 'devices': devices})
                self.assertEqual(response.status_code, 201, response.data)

        # Канал на «только Android» — как раньше
        response = create_ad(self.client, Placement.USERS_VIDEO_BANNER,
                             targeting={'countries': ['UZ'], 'devices': ['android']})
        self.assertEqual(response.status_code, 201, response.data)


@override_settings(**ADGRAM_SETTINGS)
@patch('apps.notifications.services._enqueue_push')
class AdAdGramTests(TestCase):
    """Пользователь AdGram: целые сумы, бюджет забирает carry"""

    def setUp(self):
        self.user = create_user_with_balance(username='ag_cl1', adgram_user_id='cl1')
        self.client = client_for(self.user)

    def test_create_and_top_up_whole_sums(self, _push):
        Balance.objects.filter(user=self.user).update(carry=Decimal('0.4'))
        self.assertEqual(create_ad(self.client, budget='10000.50').status_code, 400)

        ok = OperationResult(status='done', balance_after=Decimal('0'))
        with patch('apps.billing.adgram_client.debit', return_value=ok) as debit:
            response = create_ad(self.client, budget='10000')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['budget'], '10000.40')  # оплачено + carry
        self.assertEqual(debit.call_args.args[1], Decimal('10000'))

        ad_id = response.data['id']
        self.assertEqual(self.client.post(f'/api/ads/{ad_id}/budget/', {'amount': '100.50'}).status_code, 400)
        with patch('apps.billing.adgram_client.debit', return_value=ok):
            response = self.client.post(f'/api/ads/{ad_id}/budget/', {'amount': '100'})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['budget'], '10100.40')

    def test_delete_shows_whole_sums_sent_to_adgram(self, _push):
        ok = OperationResult(status='done', balance_after=Decimal('0'))
        with patch('apps.billing.adgram_client.debit', return_value=ok):
            ad_id = create_ad(self.client, budget='10000').data['id']
        Ad.objects.filter(pk=ad_id).update(spent=Decimal('0.4'))

        response = self.client.delete(f'/api/ads/{ad_id}/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['refund_amount'], '9999.60')  # точный остаток — как раньше
        self.assertEqual(response.data['refund_wallet_amount'], '9999.00')  # в AdGram — только целые сумы
        self.assertEqual(balance_of(self.user).carry, Decimal('0.6'))

        # Прошлый carry докладывается: в кошелёк может уйти больше точного остатка
        Balance.objects.filter(user=self.user).update(carry=Decimal('0.5'))
        with patch('apps.billing.adgram_client.debit', return_value=ok):
            ad_id = create_ad(self.client, budget='10000').data['id']  # бюджет забрал carry: 10000.5
        Balance.objects.filter(user=self.user).update(carry=Decimal('0.7'))
        response = self.client.delete(f'/api/ads/{ad_id}/')
        self.assertEqual((response.data['refund_amount'], response.data['refund_wallet_amount']),
                         ('10000.50', '10001.00'))
        self.assertEqual(balance_of(self.user).carry, Decimal('0.2'))


@patch('apps.notifications.services._enqueue_push')
class AdTransitionTests(TestCase):
    """Статусы, переходы, точные возвраты, пополнение, PATCH, доступ"""

    def setUp(self):
        self.user = make_local_user()
        self.client = client_for(self.user)
        self.boss = User.objects.create_superuser('boss', 'boss@test.uz', 'pass12345')

    def new_ad(self, **overrides):
        response = create_ad(self.client, **overrides)
        self.assertEqual(response.status_code, 201, response.data)
        return Ad.objects.get(pk=response.data['id'])

    def patch(self, ad, data):
        return self.client.patch(f'/api/ads/{ad.pk}/', data, format='json')

    def refresh(self, ad):
        return Ad.objects.get(pk=ad.pk)

    def test_approve_to_initial_status(self, _push):
        active = self.new_ad()
        on_hold = self.new_ad(initial_status='ON_HOLD')
        call_locked(active, 'approve', self.boss)
        call_locked(on_hold, 'approve', self.boss)
        self.assertEqual(self.refresh(active).status, AdStatus.ACTIVE)
        self.assertEqual(self.refresh(on_hold).status, AdStatus.ON_HOLD)

    def test_owner_toggles_active_and_on_hold(self, _push):
        ad = self.new_ad()
        call_locked(ad, 'approve', self.boss)
        self.assertEqual(self.patch(ad, {'status': 'ON_HOLD'}).data['status'], AdStatus.ON_HOLD)
        self.assertEqual(self.patch(ad, {'status': 'ACTIVE'}).data['status'], AdStatus.ACTIVE)

    def test_forbidden_transitions(self, _push):
        ad = self.new_ad()
        response = self.patch(ad, {'status': 'ON_HOLD'})  # до одобрения нельзя
        self.assertEqual(response.status_code, 400)
        self.assertIn('error', response.data)
        self.assertEqual(self.patch(ad, {'status': 'STOPPED'}).status_code, 400)  # только ACTIVE / ON_HOLD

        with self.assertRaises(AdStateError):
            call_locked(ad, 'stop')  # остановить можно только ACTIVE
        call_locked(ad, 'approve', self.boss)
        with self.assertRaises(AdStateError):
            call_locked(ad, 'approve', self.boss)
        with self.assertRaises(AdStateError):
            call_locked(ad, 'decline', self.boss, 'поздно')  # отклонить — только на модерации

        call_locked(ad, 'block', self.boss, 'нарушение')
        for action in (lambda: self.patch(ad, {'cpm': 2000}),
                       lambda: self.client.post(f'/api/ads/{ad.pk}/budget/', {'amount': '100'})):
            self.assertEqual(action().status_code, 400)
        with self.assertRaises(AdStateError):
            call_locked(ad, 'block', self.boss, 'ещё раз')

    def test_decline_returns_everything_exactly(self, _push):
        Balance.objects.filter(user=self.user).update(carry=Decimal('0.004'))
        ad = self.new_ad()
        self.assertEqual(ad.budget, Decimal('10000.004'))  # carry ушёл в бюджет
        before = balance_of(self.user).amount

        self.assertEqual(call_locked(ad, 'decline', self.boss, 'не подходит'), Decimal('10000.004'))
        ad = self.refresh(ad)
        self.assertEqual((ad.status, ad.moderation_reason), (AdStatus.DECLINED, 'не подходит'))
        self.assertEqual(ad.remaining_budget(), Decimal('0'))
        self.assertEqual(balance_of(self.user).amount, before + Decimal('10000.00'))
        self.assertEqual(balance_of(self.user).carry, Decimal('0.004'))  # доля копейки снова в carry
        op = Op.objects.get(kind=Op.Kind.REFUND)
        self.assertEqual((op.reason, op.exact_amount), (Op.Reason.REJECT, Decimal('10000.004')))

    def test_block_returns_remainder(self, _push):
        ad = self.new_ad()
        call_locked(ad, 'approve', self.boss)
        Ad.objects.filter(pk=ad.pk).update(spent=Decimal('1234.5678'))  # как будто выдача потратила
        self.assertEqual(call_locked(ad, 'block', self.boss, 'нарушение'), Decimal('8765.4322'))
        self.assertEqual(self.refresh(ad).status, AdStatus.BLOCKED)

    def test_delete_returns_exact_remainder(self, _push):
        ad = self.new_ad()
        Ad.objects.filter(pk=ad.pk).update(spent=Decimal('1.2345'))
        before = balance_of(self.user).amount

        response = self.client.delete(f'/api/ads/{ad.pk}/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['refund_amount'], '9998.7655')
        self.assertEqual(response.data['refund_wallet_amount'], '9998.76')  # локальный баланс — до копеек
        self.assertEqual(balance_of(self.user).amount, before + Decimal('9998.76'))
        self.assertEqual(balance_of(self.user).carry, Decimal('0.0055'))
        self.assertEqual(self.refresh(ad).status, AdStatus.DELETED)
        self.assertEqual(Op.objects.get(kind=Op.Kind.REFUND).reason, Op.Reason.DELETE)

        # Удалённое в API не видно
        self.assertEqual(self.client.get(f'/api/ads/{ad.pk}/').status_code, 404)
        self.assertEqual(self.client.delete(f'/api/ads/{ad.pk}/').status_code, 404)
        self.assertEqual(self.client.get('/api/ads/').data['count'], 0)

    def test_delete_after_decline_returns_nothing_more(self, _push):
        ad = self.new_ad()
        call_locked(ad, 'decline', self.boss, 'нет')
        response = self.client.delete(f'/api/ads/{ad.pk}/')
        self.assertEqual((response.data['refund_amount'], response.data['refund_wallet_amount']), ('0.00', '0.00'))
        self.assertEqual(Op.objects.filter(kind=Op.Kind.REFUND).count(), 1)

    def test_stopped_top_up_reactivates(self, _push):
        ad = self.new_ad()
        call_locked(ad, 'approve', self.boss)
        call_locked(ad, 'stop')
        self.assertEqual(self.refresh(ad).status, AdStatus.STOPPED)
        before = balance_of(self.user).amount

        response = self.client.post(f'/api/ads/{ad.pk}/budget/', {'amount': '5000'})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['status'], AdStatus.ACTIVE)  # без повторной модерации
        self.assertEqual(response.data['budget'], '15000.00')
        self.assertEqual(balance_of(self.user).amount, before - Decimal('5000'))
        self.assertTrue(Op.objects.filter(order_id=ad.pk, reason=Op.Reason.TOP_UP).exists())

    def test_patch_immutable_fields(self, _push):
        # Креатив и title с этапа 4 меняются (см. apps/moderation/test_ads.py) — здесь только неизменяемое
        ad = self.new_ad()
        for field, value in (('targeting', {'bots': []}), ('placement', 'SEARCH'), ('url', 't.me/daryo'),
                             ('budget', '1'), ('spent', '0')):
            with self.subTest(field=field):
                response = self.patch(ad, {field: value})
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.data)

    def test_patch_allowed_fields(self, _push):
        ad = self.new_ad()
        self.assertEqual(self.patch(ad, {'cpm': 999}).data['cpm'][0], "CPM can't be less than 1000")
        response = self.patch(ad, {'cpm': 1500, 'daily_limit': 3000, 'views_per_user_per_day': 0,
                                   'schedule': {'enabled': True, 'days': [16777215] * 7}})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data['cpm'], response.data['daily_limit'],
                          response.data['views_per_user_per_day']), (1500, 3000, 0))
        self.assertEqual(self.patch(ad, {'views_per_user_per_day': 5}).status_code, 400)

    def test_patch_dates(self, _push):
        start = (timezone.now() + timedelta(days=1)).isoformat()
        ad = self.new_ad()
        call_locked(ad, 'approve', self.boss)
        self.assertIn('start_date', self.patch(ad, {'start_date': start}).data)  # ACTIVE — старт нельзя

        response = self.patch(ad, {'status': 'ON_HOLD', 'start_date': start})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNotNone(response.data['start_date'])
        self.assertIn('end_date', self.patch(ad, {'end_date': start}).data)  # конец не позже старта

        response = self.patch(ad, {'status': 'ACTIVE'})  # запустили руками — дата старта больше не нужна
        self.assertIsNone(response.data['start_date'])

    def test_foreign_ad_is_404(self, _push):
        ad = self.new_ad()
        stranger = client_for(make_local_user('stranger'))
        for response in (
            stranger.get(f'/api/ads/{ad.pk}/'),
            stranger.patch(f'/api/ads/{ad.pk}/', {'cpm': 2000}, format='json'),
            stranger.delete(f'/api/ads/{ad.pk}/'),
            stranger.post(f'/api/ads/{ad.pk}/budget/', {'amount': '100'}),
            stranger.get(f'/api/ads/{ad.pk}/budget-history/'),
        ):
            self.assertEqual(response.status_code, 404)
        self.assertEqual(stranger.get('/api/ads/').data['count'], 0)
        self.assertEqual(self.refresh(ad).status, AdStatus.IN_REVIEW)

    def test_budget_history(self, _push):
        ad = self.new_ad()
        self.client.post(f'/api/ads/{ad.pk}/budget/', {'amount': '500'})
        response = self.client.get(f'/api/ads/{ad.pk}/budget-history/')
        self.assertEqual([(op['reason'], op['wallet_amount']) for op in response.data['results']],
                         [('TOP_UP', '500.00'), ('CREATE', '10000.00')])

    def test_pricing_endpoint(self, _push):
        response = self.client.get('/api/ads/pricing/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['min_cpm']['SEARCH'], 1000)
        self.assertEqual(set(response.data['min_cpm']), set(Placement.values))
        self.assertEqual((response.data['min_budget'], response.data['terms_version']), (10000, '1.0'))
        self.assertEqual(Decimal(response.data['channel_picture_markup_percent']), Decimal('30'))

    def test_db_does_not_allow_spending_more_than_budget(self, _push):
        ad = self.new_ad()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Ad.objects.filter(pk=ad.pk).update(spent=Decimal('10000.01'))

    def test_admin_decline_action(self, _push):
        ad = self.new_ad()
        admin_client = APIClient()
        admin_client.force_login(self.boss)
        admin_client.post('/admin/ads/ad/', {'action': 'decline_ads', '_selected_action': [str(ad.pk)]})
        ad = self.refresh(ad)
        self.assertEqual((ad.status, ad.moderation_reason), (AdStatus.DECLINED, 'Отклонено через админку'))
        self.assertEqual(ad.refunded, Decimal('10000'))

        # Карточку объявления в админке сохранить нельзя (полное сохранение без лока затёрло бы деньги)
        response = admin_client.post(f'/admin/ads/ad/{ad.pk}/change/', {'title': 'новое', 'cpm': 1})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.refresh(ad).title, 'Тест')


class AdDatesBeatTests(TestCase):
    """beat раз в минуту: наступил start_date → ACTIVE, наступил end_date → ON_HOLD"""

    def setUp(self):
        self.client = client_for(make_local_user())
        self.past = timezone.now() - timedelta(minutes=1)
        self.future = timezone.now() + timedelta(days=1)

    def ad_with(self, status, start=None, end=None):
        response = create_ad(self.client)
        Ad.objects.filter(pk=response.data['id']).update(status=status, start_date=start, end_date=end)
        return response.data['id']

    def test_dates(self):
        starts = self.ad_with(AdStatus.ON_HOLD, start=self.past)
        ends = self.ad_with(AdStatus.ACTIVE, end=self.past)
        window_passed = self.ad_with(AdStatus.ON_HOLD, start=self.past, end=self.past)
        in_review = self.ad_with(AdStatus.IN_REVIEW, start=self.past)
        not_yet = self.ad_with(AdStatus.ON_HOLD, start=self.future, end=self.future + timedelta(days=1))

        Ad.apply_due_dates()

        def state(pk):
            ad = Ad.objects.get(pk=pk)
            return ad.status, ad.start_date, ad.end_date

        self.assertEqual(state(starts), (AdStatus.ACTIVE, None, None))
        self.assertEqual(state(ends), (AdStatus.ON_HOLD, None, None))
        self.assertEqual(state(window_passed), (AdStatus.ON_HOLD, None, None))  # окно прошло — остаётся на паузе
        self.assertEqual(state(in_review)[0], AdStatus.IN_REVIEW)               # ждёт одобрения
        self.assertEqual(state(not_yet)[0], AdStatus.ON_HOLD)
