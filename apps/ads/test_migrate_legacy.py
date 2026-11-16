"""
Перенос старых заказов поиска в v2 (manage.py migrate_legacy_search): отчёт ничего не меняет; статус,
статистика и деньги переезжают точно, без движения кошелька; деньги возвращает только v2 и только раз;
повторный запуск безопасен; заказ, который не сможет крутиться в v2, не трогается
"""
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db.models import F
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.ads.management.commands.migrate_legacy_search import cpm_from, queries_from
from apps.ads.models import Ad, AdDailyStat, AdStatus, Placement
from apps.ads.tests import (KUNUZ_ID, call_locked, client_for, ensure_test_peers, make_local_user,  # noqa: F401
                            setUpModule, tearDownModule)
from apps.billing.models import Balance, MoneyOperation as Op
from apps.common.models import ModerationStatus
from apps.directories.models import TelegramPeer
from apps.moderation.test_ads import make_moderator
from apps.search_ads.models import Order


class MigrateLegacySearchTests(TestCase):

    def setUp(self):
        ensure_test_peers()
        push = patch('apps.notifications.services._enqueue_push')
        push.start()
        self.addCleanup(push.stop)
        self.boss = make_local_user('boss')
        self.boss.is_superuser = True
        self.boss.save(update_fields=['is_superuser'])
        self.boss_client = client_for(self.boss)

    def old_order(self, client=None, spm='5000.00', budget='100.00', tags=('новости uz',), channel='kunuz',
                  channel_id=None, max_views=13, shown=0, clicks=0, **state):
        """Старый заказ поиска через старый API (у суперадмина — сразу одобрен); state — поля поверх"""
        before = set(Order.objects.values_list('pk', flat=True))
        response = (client or self.boss_client).post('/api/search_ads/order/create/', {
            'channel_id': channel_id or f'id_{channel}', 'channel_name': channel, 'order_name': 'Eski',
            'tags': list(tags), 'spm': spm, 'budget': budget, 'max_views_per_user': max_views,
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        order = Order.objects.exclude(pk__in=before).get()
        updates = {'shown_views': F('shown_views') + shown, 'clicks': clicks,
                   'remaining_views': F('remaining_views') - shown, **state}
        Order.objects.filter(pk=order.pk).update(**updates)
        return Order.objects.get(pk=order.pk)

    def run_command(self, *args):
        out = StringIO()
        call_command('migrate_legacy_search', *args, stdout=out, stderr=StringIO())
        return out.getvalue()

    @staticmethod
    def money(user):
        balance = Balance.objects.get(user=user)
        return balance.amount, balance.carry

    def test_dry_run_changes_nothing(self):
        order = self.old_order(shown=2)
        money, ops = self.money(self.boss), Op.objects.count()
        out = self.run_command()
        self.assertIn("Ko'chiriladi: 1 ta — ACTIVE 1", out)
        self.assertIn('faqat hisobot', out)
        self.assertFalse(Ad.objects.exists())
        order.refresh_from_db()
        self.assertEqual((order.cancelled, order.is_active, order.refunded), (False, True, Decimal('0')))
        self.assertEqual((self.money(self.boss), Op.objects.count()), (money, ops))

    def test_live_order_moves_with_stats_and_money(self):
        order = self.old_order(spm='5000.00', budget='100.00', tags=('новости uz', 'abc', 'Новости UZ'),
                               shown=2, clicks=3)
        money = self.money(self.boss)
        self.run_command('--apply')

        ad = Ad.objects.get()
        self.assertEqual((ad.placement, ad.status, ad.cpm, ad.views_per_user_per_day),
                         (Placement.SEARCH, AdStatus.ACTIVE, 5000, 4))
        self.assertEqual((ad.budget, ad.spent, ad.refunded, ad.remaining_budget()),
                         (Decimal('100'), Decimal('10'), Decimal('0'), Decimal('90')))
        self.assertEqual((ad.views_total, ad.clicks_total), (2, 3))
        self.assertEqual(ad.targeting, {'queries': ['новости uz']})  # 'abc' короче 4, дубль — без учёта регистра
        self.assertEqual((ad.url, ad.promoted_peer.username, ad.user), ('https://t.me/kunuz', 'kunuz', self.boss))
        self.assertEqual((ad.legacy_order_type, ad.legacy_order_id, ad.created_at),
                         ('search_ads', order.pk, order.created_at))
        stat = AdDailyStat.objects.get(ad=ad)
        self.assertEqual((stat.date, stat.views, stat.clicks, stat.spent),
                         (timezone.localdate(order.created_at), 2, 3, Decimal('10')))

        order.refresh_from_db()
        self.assertEqual((order.status, order.cancelled, order.is_active, order.remaining_views),
                         (ModerationStatus.APPROVED, True, False, 0))
        self.assertEqual(order.unrefunded_amount(), Decimal('0'))  # остаток теперь у объявления
        self.assertEqual(self.money(self.boss), money)  # кошелёк не двигался
        refund = Op.objects.get(order_type='search_ads', order_id=order.pk, kind=Op.Kind.REFUND)
        debit = Op.objects.get(order_type='ads', order_id=ad.pk)
        for op in (refund, debit):
            self.assertEqual((op.reason, op.exact_amount, op.wallet_amount, op.status),
                             (Op.Reason.MIGRATE, Decimal('90'), Decimal('0'), Op.Status.DONE))
        self.assertEqual(debit.kind, Op.Kind.DEBIT)

    def test_statuses(self):
        adv = make_local_user('adv')
        orders = {
            AdStatus.ON_HOLD: self.old_order(is_active=False),
            AdStatus.STOPPED: self.old_order(shown=20, completed=True, is_active=False),  # 100 / 5 = 20 показов
            AdStatus.IN_REVIEW: self.old_order(client=client_for(adv), channel='daryo'),  # рекламодатель — PENDING
            AdStatus.DECLINED: self.old_order(status=ModerationStatus.REJECTED, is_active=False, remaining_views=0,
                                              refunded=Decimal('100')),
            AdStatus.BLOCKED: self.old_order(status=ModerationStatus.BLOCKED, is_active=False, remaining_views=0,
                                             refunded=Decimal('100')),
            AdStatus.DELETED: self.old_order(cancelled=True, is_active=False, remaining_views=0,
                                             refunded=Decimal('100')),
        }
        self.run_command('--apply')
        for status, order in orders.items():
            with self.subTest(status=status):
                self.assertEqual(Ad.objects.get(legacy_order_id=order.pk).status, status)

    def test_money_returned_once_by_v2(self):
        adv = make_local_user('adv')
        order = self.old_order(client=client_for(adv), channel='daryo')  # PENDING → IN_REVIEW, в заказе 100
        after_order = self.money(adv)
        self.run_command('--apply')
        order.refresh_from_db()
        self.assertEqual(order.cancel_order(), 0)  # старый путь больше ничего не вернёт
        ad = Ad.objects.get()
        self.assertEqual(call_locked(ad, 'decline', make_moderator(), 'Перенесён, отклонён'), Decimal('100'))
        self.assertEqual(self.money(adv)[0], after_order[0] + Decimal('100'))  # вернул v2, один раз

    def test_idempotent_and_limit(self):
        self.old_order()
        self.old_order()
        self.run_command('--apply', '--limit', '1')
        self.assertEqual(Ad.objects.count(), 1)
        self.run_command('--apply')
        self.run_command('--apply')
        self.assertEqual(Ad.objects.count(), 2)
        self.assertFalse(Order.objects.filter(cancelled=False).exists())

    def test_order_that_cannot_run_in_v2_is_untouched(self):
        short_tags = self.old_order(tags=('abc',))  # живой, но фраз нет
        no_username = self.old_order(channel='Новостной канал', channel_id='-1001234567')
        archived = self.old_order(tags=('abc',), cancelled=True, is_active=False, remaining_views=0,
                                  refunded=Decimal('100'))  # архиву фразы не нужны

        out = self.run_command('--apply')
        for order, reason in ((short_tags, "4–32 belgili teg yo'q"), (no_username, "kanalning username'i yo'q")):
            self.assertIn(f'{order.pk}', out)
            self.assertIn(reason, out)
            order.refresh_from_db()
            self.assertEqual((order.cancelled, order.refunded), (False, Decimal('0')))
            self.assertFalse(Ad.objects.filter(legacy_order_id=order.pk).exists())
        ad = Ad.objects.get(legacy_order_id=archived.pk)
        self.assertEqual((ad.status, ad.targeting), (AdStatus.DELETED, {'queries': []}))


    def test_channel_id_mismatch_not_promoted(self):
        """Старый id канала ведёт не туда, куда username: живой заказ не переносим (рекламировали бы чужой канал)"""
        live = self.old_order(channel_id='-1009999999999')  # у kunuz id другой
        archived = self.old_order(channel_id='-1009999999999', cancelled=True, is_active=False, remaining_views=0,
                                  refunded=Decimal('100'))
        out = self.run_command('--apply')
        self.assertIn("eski kanal id'si username'dagi kanalga mos emas", out)
        self.assertFalse(Ad.objects.filter(legacy_order_id=live.pk).exists())
        live.refresh_from_db()
        self.assertEqual((live.cancelled, live.is_active), (False, True))
        self.assertIsNone(Ad.objects.get(legacy_order_id=archived.pk).promoted_peer)  # архив — без чужого канала

    def test_user_account_only_for_superadmin(self):
        """Заказ, рекламирующий аккаунт пользователя: у суперадмина переносится (кнопка USER), у других — нет"""
        for name in ('boss_admin', 'adv_admin'):
            TelegramPeer.objects.create(username=name, peer_type=TelegramPeer.PeerType.USER, title=name,
                                        fetched_at=timezone.now())
        boss_order = self.old_order(channel='boss_admin', channel_id='-1008728334742')  # id «канала» — мусор
        adv = make_local_user('adv')
        adv_order = self.old_order(client=client_for(adv), channel='adv_admin')

        out = self.run_command('--apply')
        self.assertIn('akkaunt reklama qilinadi (kanal emas): @boss_admin — 1 ta', out)
        ad = Ad.objects.get(legacy_order_id=boss_order.pk)
        self.assertEqual((ad.status, ad.promoted_type, ad.action_type), (AdStatus.ACTIVE, 'USER', 'MESSAGE'))
        self.assertFalse(Ad.objects.filter(legacy_order_id=adv_order.pk).exists())

    def test_channel_id_match_is_fine(self):
        order = self.old_order(channel_id=str(KUNUZ_ID))
        self.run_command('--apply')
        ad = Ad.objects.get(legacy_order_id=order.pk)
        self.assertEqual((ad.status, ad.promoted_peer.telegram_id), (AdStatus.ACTIVE, KUNUZ_ID))


class MigrateHelpersTests(SimpleTestCase):
    def test_cpm_from_spm(self):
        self.assertEqual([cpm_from(Decimal(value)) for value in ('200000.00', '2.50', '2.49', '0.40')],
                         [200000, 3, 2, 1])

    def test_queries_from_tags(self):
        self.assertEqual(queries_from([' tabr ', 'tab', 'Tabr', 'x' * 33, "tug'ilgan kuning bilan"]),
                         ['tabr', "tug'ilgan kuning bilan"])
