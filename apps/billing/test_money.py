"""
Тесты общего баланса с AdGram: списание при создании заказа, точные возвраты,
дробный остаток (carry), добивание PENDING, компенсации, журнал операций.
AdGram замокан — в сеть тесты не ходят.
"""
import importlib
import threading
import uuid
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.account.services import create_user_with_balance
from apps.billing import services
from apps.billing.adgram_client import AdGramRejected, AdGramUnavailable, OperationResult, WalletBalance
from apps.billing.models import Balance, MoneyOperation
from apps.chat_ads.models import ChatAdOrder
from apps.common.models import ModerationStatus
from apps.notifications.models import Notification
from apps.search_ads.models import Order

User = get_user_model()

ADGRAM_SETTINGS = {'ADGRAM_INTERNAL_URL': 'http://adgram.test', 'ADGRAM_INTERNAL_KEY': 'test-key'}
Op = MoneyOperation


def ok(balance_after='0', status='done'):
    """Успешный ответ AdGram на debit/refund"""
    return OperationResult(status=status, balance_after=Decimal(balance_after))


def make_adgram_user(adgram_user_id='cladgram0001'):
    return create_user_with_balance(username=f'ag_{adgram_user_id}', adgram_user_id=adgram_user_id)


def make_local_user(username='local', amount='100.00'):
    user = User.objects.create_user(username, password='pass12345')
    Balance.objects.create(user=user, amount=Decimal(amount))
    return user


def make_moderator():
    return User.objects.create_user('moder', password='pass12345', role=User.Role.MODERATOR)


def client_for(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def create_search_order(client, budget, spm='5.00'):
    return client.post('/api/search_ads/order/create/', {
        'channel_id': f'ch_{uuid.uuid4().hex[:8]}',
        'channel_name': f'Канал {uuid.uuid4().hex[:8]}',
        'order_name': 'Тест', 'tags': ['новости'],
        'spm': spm, 'budget': budget, 'max_views_per_user': 1,
    }, format='json')


def create_chat_order(client, budget, spm='5.00'):
    return client.post('/api/chat_ads/order/create/', {
        'order_name': 'Тест', 'text': 'Текст', 'link': 'https://example.com',
        'channels': 'testchan', 'spm': spm, 'budget': budget,
    }, format='json')


def set_carry(user, carry):
    Balance.objects.filter(user=user).update(carry=Decimal(carry))


def carry_of(user):
    return Balance.objects.get(user=user).carry


def shift_back(op, minutes):
    """Сдвигаем время операции в прошлое (как будто она висит давно)"""
    past = timezone.now() - timedelta(minutes=minutes)
    Op.objects.filter(pk=op.pk).update(created_at=past, updated_at=past)


def send_now(operation_id):
    """Вместо Celery — отправляем сразу (AdGram при этом замокан)"""
    services.send_operation(operation_id)


def remaining_after(order, shown):
    """remaining_views после shown показов (как их списывает горячий путь)"""
    return order.total_views - shown


def refund_locked(order, reason=Op.Reason.CANCEL):
    """Возврат так, как его делают вьюхи: в транзакции, под select_for_update"""
    with transaction.atomic():
        locked = type(order).objects.select_for_update().get(pk=order.pk)
        return services.refund(locked, reason)


@override_settings(**ADGRAM_SETTINGS)
@patch('apps.billing.services.enqueue_send', side_effect=send_now)
class AdGramChargeTests(TestCase):
    """Списание с кошелька AdGram при создании заказа"""

    def setUp(self):
        self.user = make_adgram_user()
        self.client = client_for(self.user)

    def test_debit_ok_creates_order_with_carry_in_budget(self, _send):
        set_carry(self.user, '0.4')
        with patch('apps.billing.adgram_client.debit', return_value=ok('9000')) as debit:
            response = create_search_order(self.client, '1000')
        self.assertEqual(response.status_code, 201)

        order = Order.objects.get(user=self.user)
        op = Op.objects.get(kind=Op.Kind.DEBIT)
        debit.assert_called_once()
        self.assertEqual(debit.call_args.args[:3], (self.user.adgram_user_id, Decimal('1000'), op.pk))

        # Бюджет = оплачено + carry, carry ушёл целиком
        self.assertEqual(order.budget, Decimal('1000.4'))
        self.assertEqual(carry_of(self.user), Decimal('0'))
        self.assertEqual(op.status, Op.Status.DONE)
        self.assertEqual(op.order_id, order.pk)
        self.assertEqual(op.wallet, Op.Wallet.ADGRAM)
        self.assertEqual(op.carry_delta, Decimal('-0.4'))
        self.assertEqual(op.balance_after, Decimal('9000'))

    def test_chat_order_is_paid_from_adgram_too(self, _send):
        with patch('apps.billing.adgram_client.debit', return_value=ok('500')):
            response = create_chat_order(self.client, '500')
        self.assertEqual(response.status_code, 201)
        order = ChatAdOrder.objects.get(user=self.user)
        self.assertEqual(Op.objects.get(kind=Op.Kind.DEBIT).order_id, order.pk)

    def test_not_enough_money_402(self, _send):
        set_carry(self.user, '0.4')
        with patch('apps.billing.adgram_client.debit', side_effect=AdGramRejected(402, {'balanceUzs': 300})):
            response = create_search_order(self.client, '1000')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['budget'], 'Недостаточно средств на балансе AdGram')
        self.assertEqual(response.data['balance'], '300.40')  # как в /api/balance/: AdGram + carry
        self.assertEqual(Op.objects.get().status, Op.Status.FAILED)
        self.assertFalse(Order.objects.exists())
        self.assertEqual(carry_of(self.user), Decimal('0.4'))  # carry не тронут

    def test_blocked_user_403(self, _send):
        with patch('apps.billing.adgram_client.debit', side_effect=AdGramRejected(403, {})):
            response = create_search_order(self.client, '1000')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Op.objects.get().status, Op.Status.FAILED)
        self.assertFalse(Order.objects.exists())

    def test_timeout_pending_then_retry_done_then_compensation(self, _send):
        """Таймаут → PENDING → beat повторяет тем же operationId → DONE → заказа нет → компенсация"""
        with patch('apps.billing.adgram_client.debit', side_effect=AdGramUnavailable('timeout')):
            response = create_search_order(self.client, '1000')
        self.assertEqual(response.status_code, 503)
        debit_op = Op.objects.get()
        self.assertEqual(debit_op.status, Op.Status.PENDING)
        self.assertEqual(debit_op.attempts, 1)
        self.assertFalse(Order.objects.exists())

        # Свежую операцию beat не трогает
        with patch('apps.billing.adgram_client.debit') as debit:
            services.retry_pending_operations()
        debit.assert_not_called()

        # Через 2 минуты — повтор тем же operationId, AdGram: «уже было»
        shift_back(debit_op, minutes=2)
        with patch('apps.billing.adgram_client.debit', return_value=ok('8000', 'already_done')) as debit:
            services.retry_pending_operations()
        self.assertEqual(debit.call_args.args[2], debit_op.pk)
        debit_op.refresh_from_db()
        self.assertEqual(debit_op.status, Op.Status.DONE)
        self.assertEqual(debit_op.attempts, 2)
        self.assertIsNone(debit_op.order_id)

        # Раньше 10 минут не компенсируем
        services.compensate_orphan_debits()
        self.assertEqual(Op.objects.count(), 1)

        # Через 11 минут — компенсирующий возврат ровно на списанную сумму, один раз
        shift_back(debit_op, minutes=11)
        with patch('apps.billing.adgram_client.refund', return_value=ok('9000')) as refund:
            services.compensate_orphan_debits()
            services.compensate_orphan_debits()
        compensation = Op.objects.get(reason=Op.Reason.COMPENSATION)
        self.assertEqual(compensation.compensates, debit_op)
        self.assertEqual(compensation.kind, Op.Kind.REFUND)
        self.assertEqual(compensation.status, Op.Status.DONE)
        self.assertEqual(compensation.wallet_amount, Decimal('1000'))
        refund.assert_called_once()
        self.assertEqual(refund.call_args.args[:3], (self.user.adgram_user_id, Decimal('1000'), compensation.pk))

    def test_order_not_created_after_debit_is_compensated(self, _send):
        """Деньги списались, а заказ упал при создании — списание без заказа вернётся"""
        set_carry(self.user, '0.4')

        def broken_create_order(budget):
            raise RuntimeError('упало создание заказа')

        with patch('apps.billing.adgram_client.debit', return_value=ok('9000')):
            with self.assertRaises(RuntimeError):
                services.charge(self.user, Decimal('1000'), Op.OrderType.SEARCH_ADS, broken_create_order)

        debit_op = Op.objects.get()
        self.assertEqual(debit_op.status, Op.Status.DONE)
        self.assertIsNone(debit_op.order_id)
        self.assertIn('Заказ не создан', debit_op.last_error)
        self.assertEqual(carry_of(self.user), Decimal('0.4'))  # транзакция заказа откатилась вместе с carry

        shift_back(debit_op, minutes=11)
        with patch('apps.billing.adgram_client.refund', return_value=ok('10000')):
            services.compensate_orphan_debits()
        self.assertEqual(Op.objects.get(reason=Op.Reason.COMPENSATION).status, Op.Status.DONE)

    @override_settings(ADGRAM_INTERNAL_URL='', ADGRAM_INTERNAL_KEY='')
    def test_not_configured_503(self, _send):
        with patch('apps.billing.adgram_client.debit') as debit:
            response = create_search_order(self.client, '1000')
        self.assertEqual(response.status_code, 503)
        debit.assert_not_called()
        self.assertFalse(Op.objects.exists())

    def test_budget_must_be_whole_sums(self, _send):
        with patch('apps.billing.adgram_client.debit') as debit:
            search = create_search_order(self.client, '1000.50')
            chat = create_chat_order(self.client, '1000.50')
        self.assertEqual(search.status_code, 400)
        self.assertIn('budget', search.data)
        self.assertEqual(chat.status_code, 400)
        self.assertIn('budget', chat.data)
        debit.assert_not_called()

    def test_config_error_alerts_superadmin(self, _send):
        """400/401 от AdGram при списании — ошибка настройки: FAILED + уведомление суперадмину"""
        boss = User.objects.create_superuser('boss', 'boss@test.uz', 'pass12345')
        with patch('apps.billing.adgram_client.debit', side_effect=AdGramRejected(401, {})):
            response = create_search_order(self.client, '1000')
        self.assertEqual(response.status_code, 503)
        op = Op.objects.get()
        self.assertEqual(op.status, Op.Status.FAILED)
        self.assertIsNotNone(op.alerted_at)
        self.assertTrue(Notification.objects.filter(user=boss, type=Notification.Type.MONEY_ALERT).exists())


@override_settings(**ADGRAM_SETTINGS)
@patch('apps.billing.services.enqueue_send', side_effect=send_now)
class RefundTests(TestCase):
    """Возвраты: точный остаток, carry, повторные события, завершённые заказы"""

    def setUp(self):
        self.adgram_user = make_adgram_user()
        self.moderator = make_moderator()
        # Push о решении модератора в тестах не шлём (иначе Celery стучится в Redis)
        push = patch('apps.notifications.services._enqueue_push')
        push.start()
        self.addCleanup(push.stop)

    def _adgram_order(self, budget, spm='5.00', chat=False):
        create = create_chat_order if chat else create_search_order
        with patch('apps.billing.adgram_client.debit', return_value=ok('0')):
            response = create(client_for(self.adgram_user), budget, spm)
        self.assertEqual(response.status_code, 201)
        model = ChatAdOrder if chat else Order
        return model.objects.filter(user=self.adgram_user).latest('created_at')

    def test_reject_returns_whole_budget_including_carry(self, _send):
        set_carry(self.adgram_user, '0.4')
        order = self._adgram_order('1000')
        self.assertEqual(order.budget, Decimal('1000.4'))

        with patch('apps.billing.adgram_client.refund', return_value=ok('1000')) as refund:
            with self.captureOnCommitCallbacks(execute=True):
                response = client_for(self.moderator).post(
                    f'/api/moderation/search_ads/{order.pk}/reject/', {'reason': 'не подходит'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Decimal(response.data['refund_amount']), Decimal('1000.4'))

        # В AdGram ушли целые сумы, доля сума вернулась в carry — ничего не потерялось
        op = Op.objects.get(kind=Op.Kind.REFUND)
        refund.assert_called_once()
        self.assertEqual(refund.call_args.args[:3], (self.adgram_user.adgram_user_id, Decimal('1000'), op.pk))
        self.assertEqual(op.reason, Op.Reason.REJECT)
        self.assertEqual(op.status, Op.Status.DONE)
        self.assertEqual(op.exact_amount, Decimal('1000.4'))
        self.assertEqual(carry_of(self.adgram_user), Decimal('0.4'))
        order.refresh_from_db()
        self.assertEqual(order.refunded, Decimal('1000.4'))

    def test_block_returns_exact_remainder_to_adgram(self, _send):
        order = self._adgram_order('1000', spm='3.00', chat=True)
        ChatAdOrder.objects.filter(pk=order.pk).update(shown_views=1000, remaining_views=remaining_after(order, 1000))

        with patch('apps.billing.adgram_client.refund', return_value=ok('997')) as refund:
            with self.captureOnCommitCallbacks(execute=True):
                response = client_for(self.moderator).post(
                    f'/api/moderation/chat_ads/{order.pk}/block/', {'reason': 'нарушение'}, format='json')
        self.assertEqual(response.status_code, 200)
        # потрачено 1000 × 3 / 1000 = 3 → вернули ровно 997
        self.assertEqual(Decimal(response.data['refund_amount']), Decimal('997'))
        self.assertEqual(refund.call_args.args[1], Decimal('997'))
        self.assertEqual(Op.objects.get(kind=Op.Kind.REFUND).reason, Op.Reason.BLOCK)

    def test_cancel_returns_exact_remainder_with_carry_locally(self, _send):
        """Локальный кошелёк: в баланс — до копейки вниз, доли копейки — в carry"""
        user = make_local_user(amount='100.00')
        response = create_search_order(client_for(user), '100.00', spm='7.77')
        self.assertEqual(response.status_code, 201)
        order = Order.objects.get(user=user)
        Order.objects.filter(pk=order.pk).update(shown_views=7, remaining_views=remaining_after(order, 7))

        response = client_for(user).post(f'/api/search_ads/order/{order.pk}/cancel/')
        self.assertEqual(response.status_code, 200)

        # потрачено 7 × 7.77 / 1000 = 0.05439 → остаток 99.94561
        op = Op.objects.get(kind=Op.Kind.REFUND)
        self.assertEqual(op.reason, Op.Reason.CANCEL)
        self.assertEqual(op.wallet, Op.Wallet.LOCAL)
        self.assertEqual(op.exact_amount, Decimal('99.94561'))
        self.assertEqual(op.wallet_amount, Decimal('99.94'))
        self.assertEqual(op.carry_delta, Decimal('0.00561'))
        self.assertEqual(Balance.objects.get(user=user).amount, Decimal('99.94'))
        self.assertEqual(carry_of(user), Decimal('0.00561'))

    def test_repeated_refund_returns_zero(self, _send):
        user = make_local_user(amount='100.00')
        create_search_order(client_for(user), '50.00')
        order = Order.objects.get(user=user)

        self.assertEqual(refund_locked(order), Decimal('50'))
        self.assertEqual(refund_locked(order, Op.Reason.BLOCK), Decimal('0'))
        self.assertEqual(Op.objects.filter(kind=Op.Kind.REFUND).count(), 1)
        self.assertEqual(Balance.objects.get(user=user).amount, Decimal('100.00'))

    def test_carry_accumulates_and_goes_out_at_one_sum(self, _send):
        """Доли сума копятся в carry; набежал целый сум — уходит в AdGram со следующим возвратом"""
        first = self._adgram_order('10', spm='3.00')
        second = self._adgram_order('10', spm='3.00')
        for order in (first, second):
            Order.objects.filter(pk=order.pk).update(shown_views=1, remaining_views=remaining_after(order, 1))

        with patch('apps.billing.adgram_client.refund', return_value=ok('0')) as refund:
            with self.captureOnCommitCallbacks(execute=True):
                refund_locked(first)  # 10 − 0.003 = 9.997 → в AdGram 9, carry 0.997
            self.assertEqual(refund.call_args.args[1], Decimal('9'))
            self.assertEqual(carry_of(self.adgram_user), Decimal('0.997'))

            with self.captureOnCommitCallbacks(execute=True):
                refund_locked(second)  # 9.997 + 0.997 = 10.994 → в AdGram 10, carry 0.994
            self.assertEqual(refund.call_args.args[1], Decimal('10'))
            self.assertEqual(carry_of(self.adgram_user), Decimal('0.994'))

        # Новый заказ забирает carry в бюджет целиком
        third = self._adgram_order('100')
        self.assertEqual(third.budget, Decimal('100.994'))
        self.assertEqual(carry_of(self.adgram_user), Decimal('0'))

    def test_completed_order_leftover_is_returned(self, _send):
        """Завершённый заказ: дробный остаток бюджета (терялся из-за int()) возвращается beat'ом"""
        user = make_local_user(amount='100.00')
        create_search_order(client_for(user), '100.00', spm='30.00')
        order = Order.objects.get(user=user)
        self.assertEqual(order.total_views, 3333)
        Order.objects.filter(pk=order.pk).update(
            shown_views=3333, remaining_views=0, completed=True, is_active=False,
            status=ModerationStatus.APPROVED)

        services.refund_completed_orders()
        services.refund_completed_orders()  # второй прогон — уже нечего возвращать

        op = Op.objects.get(kind=Op.Kind.REFUND)
        self.assertEqual(op.reason, Op.Reason.COMPLETED)
        self.assertEqual(op.exact_amount, Decimal('0.01'))  # 100 − 3333 × 30 / 1000
        self.assertEqual(Balance.objects.get(user=user).amount, Decimal('0.01'))

    def test_completed_adgram_fraction_goes_to_carry_without_request(self, _send):
        order = self._adgram_order('1000', spm='3.00')
        Order.objects.filter(pk=order.pk).update(
            shown_views=333333, remaining_views=0, completed=True, is_active=False,
            status=ModerationStatus.APPROVED)

        with patch('apps.billing.adgram_client.refund') as refund:
            with self.captureOnCommitCallbacks(execute=True):
                services.refund_completed_orders()
        refund.assert_not_called()  # целого сума не набралось — в AdGram слать нечего
        op = Op.objects.get(kind=Op.Kind.REFUND)
        self.assertEqual(op.status, Op.Status.DONE)
        self.assertEqual(op.wallet_amount, Decimal('0'))
        self.assertEqual(carry_of(self.adgram_user), Decimal('0.001'))

    def test_refund_waits_when_adgram_not_configured(self, _send):
        """Нет настроек AdGram — возврат копится PENDING и уходит, когда настройки появились"""
        order = self._adgram_order('1000')
        with override_settings(ADGRAM_INTERNAL_URL='', ADGRAM_INTERNAL_KEY=''):
            with patch('apps.billing.adgram_client.refund') as refund:
                with self.captureOnCommitCallbacks(execute=True):
                    refund_locked(order)
            refund.assert_not_called()
        op = Op.objects.get(kind=Op.Kind.REFUND)
        self.assertEqual(op.status, Op.Status.PENDING)
        self.assertEqual(op.attempts, 0)

        shift_back(op, minutes=2)
        with patch('apps.billing.adgram_client.refund', return_value=ok('1000')):
            services.retry_pending_operations()
        op.refresh_from_db()
        self.assertEqual(op.status, Op.Status.DONE)


@override_settings(**ADGRAM_SETTINGS)
class LocalWalletTests(TestCase):
    """Пользователи без AdGram — всё как раньше, плюс журнал операций"""

    def test_create_and_cancel_order(self):
        user = make_local_user(amount='100.00')
        response = create_search_order(client_for(user), '60.00')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Balance.objects.get(user=user).amount, Decimal('40.00'))
        debit = Op.objects.get(kind=Op.Kind.DEBIT)
        self.assertEqual((debit.wallet, debit.status, debit.wallet_amount),
                         (Op.Wallet.LOCAL, Op.Status.DONE, Decimal('60.00')))

        order = Order.objects.get(user=user)
        client_for(user).post(f'/api/search_ads/order/{order.pk}/cancel/')
        self.assertEqual(Balance.objects.get(user=user).amount, Decimal('100.00'))
        self.assertEqual(Op.objects.get(kind=Op.Kind.REFUND).status, Op.Status.DONE)

    def test_not_enough_money(self):
        user = make_local_user(amount='10.00')
        response = create_chat_order(client_for(user), '60.00')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ChatAdOrder.objects.exists())
        self.assertFalse(Op.objects.exists())


class ParallelRefundTests(TransactionTestCase):
    """Гонка: отмена и блокировка одного заказа одновременно — деньги вернутся один раз"""

    @patch('apps.notifications.services._enqueue_push')
    def test_cancel_and_block_at_the_same_time(self, _push):
        user = make_local_user(amount='100.00')
        moderator = make_moderator()
        create_search_order(client_for(user), '100.00')
        order_pk = Order.objects.get(user=user).pk

        barrier = threading.Barrier(2)
        results = {}

        def cancel():
            try:
                barrier.wait()
                with transaction.atomic():
                    results['cancel'] = Order.objects.select_for_update().get(pk=order_pk).cancel_order()
            finally:
                connection.close()

        def block():
            try:
                barrier.wait()
                with transaction.atomic():
                    results['block'] = Order.objects.select_for_update().get(pk=order_pk).block(moderator, 'x')
            finally:
                connection.close()

        threads = [threading.Thread(target=cancel), threading.Thread(target=block)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(Op.objects.filter(kind=Op.Kind.REFUND).count(), 1)
        self.assertEqual(Balance.objects.get(user=user).amount, Decimal('100.00'))
        # Один из двоих вернул деньги, второй — ничего (0 у отмены / None у блокировки)
        self.assertEqual(sorted(str(value) for value in results.values() if value), ['100.000000'])


@override_settings(**ADGRAM_SETTINGS)
class BalanceAndDepositTests(TestCase):
    """/api/balance/ и пополнение суперадмином"""

    def setUp(self):
        self.adgram_user = make_adgram_user()
        self.boss = User.objects.create_superuser('boss', 'boss@test.uz', 'pass12345')

    def test_adgram_balance_is_wallet_plus_carry(self):
        set_carry(self.adgram_user, '0.4')
        with patch('apps.billing.adgram_client.balance', return_value=WalletBalance(Decimal('5000'), True)):
            response = client_for(self.adgram_user).get('/api/balance/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['amount'], '5000.40')

    def test_adgram_balance_unavailable_503(self):
        with patch('apps.billing.adgram_client.balance', side_effect=AdGramUnavailable('timeout')):
            response = client_for(self.adgram_user).get('/api/balance/')
        self.assertEqual(response.status_code, 503)

    def test_balance_without_row_is_zero(self):
        """У суперадмина из createsuperuser строки баланса нет — создаётся, ответ 0.00"""
        response = client_for(self.boss).get('/api/balance/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['amount'], '0.00')

    def test_local_balance_as_before(self):
        user = make_local_user(amount='100.00')
        response = client_for(user).get('/api/balance/')
        self.assertEqual(response.data['amount'], '100.00')
        self.assertEqual(response.data['username'], 'local')

    def test_deposit_refused_for_adgram_user(self):
        response = client_for(self.boss).post('/api/admin/balance/deposit/', {
            'user_id': str(self.adgram_user.user_id), 'amount': '50.00'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['error'], 'Пополнение только в AdGram')
        self.assertEqual(Balance.objects.get(user=self.adgram_user).amount, Decimal('0'))

    def test_local_deposit_is_logged(self):
        user = make_local_user(amount='10.00')
        response = client_for(self.boss).post('/api/admin/balance/deposit/', {
            'user_id': str(user.user_id), 'amount': '50.00'}, format='json')
        self.assertEqual(response.status_code, 200)
        op = Op.objects.get()
        self.assertEqual((op.kind, op.reason, op.balance_after),
                         (Op.Kind.DEPOSIT, Op.Reason.ADMIN_DEPOSIT, Decimal('60.00')))

    def test_django_admin_add_amount(self):
        """add_amount в админке: юзеру AdGram — отказ, локальному — пополнение и запись в журнал"""
        local = make_local_user(amount='10.00')
        client = APIClient()
        client.force_login(self.boss)

        for user in (self.adgram_user, local):
            balance = Balance.objects.get(user=user)
            client.post(f'/admin/billing/balance/{balance.pk}/change/', {
                'add_amount': '50.00', 'expected_amount': str(balance.amount)})

        self.assertEqual(Balance.objects.get(user=self.adgram_user).amount, Decimal('0'))
        self.assertEqual(Balance.objects.get(user=local).amount, Decimal('60.00'))
        op = Op.objects.get()
        self.assertEqual((op.user, op.kind), (local, Op.Kind.DEPOSIT))


class AlertTests(TestCase):
    def test_stuck_operation_alerts_superadmin_once(self):
        boss = User.objects.create_superuser('boss', 'boss@test.uz', 'pass12345')
        user = make_adgram_user()
        op = Op.objects.create(user=user, kind=Op.Kind.REFUND, reason=Op.Reason.CANCEL,
                               wallet=Op.Wallet.ADGRAM, exact_amount=Decimal('5'), wallet_amount=Decimal('5'))
        shift_back(op, minutes=120)

        services.alert_stuck_operations()
        services.alert_stuck_operations()
        self.assertEqual(Notification.objects.filter(user=boss, type=Notification.Type.MONEY_ALERT).count(), 1)


class LegacyRefundTests(TestCase):
    """Прошлые потери старой формулы: data-миграция и команда legacy_refund_report"""

    def _legacy_cancelled_order(self):
        """Старый отменённый заказ: 1000 показов из 3333, вернули по старой формуле 69.99"""
        user = make_local_user(amount='100.00')
        create_search_order(client_for(user), '100.00', spm='30.00')
        order = Order.objects.get(user=user)
        Order.objects.filter(pk=order.pk).update(
            shown_views=1000, remaining_views=0, cancelled=True, is_active=False, refunded=Decimal('69.99'))
        return user, order

    def test_data_migration_fills_refunded_by_old_formula(self):
        user = make_local_user(amount='1000.00')
        for _ in range(3):
            create_search_order(client_for(user), '100.00', spm='30.00')
        cancelled_no_views, blocked, active = Order.objects.order_by('created_at')
        Order.objects.filter(pk=cancelled_no_views.pk).update(cancelled=True)
        Order.objects.filter(pk=blocked.pk).update(status=ModerationStatus.BLOCKED, shown_views=1000)
        Order.objects.update(refunded=0)

        migration = importlib.import_module('apps.search_ads.migrations.0004_order_budget_refunded')
        migration.fill_refunded(django_apps, None)

        self.assertEqual(Order.objects.get(pk=cancelled_no_views.pk).refunded, Decimal('100'))  # весь бюджет
        self.assertEqual(Order.objects.get(pk=blocked.pk).refunded, Decimal('69.99'))  # 2333 × 30 / 1000 вниз
        self.assertEqual(Order.objects.get(pk=active.pk).refunded, Decimal('0'))  # живой — не трогаем

    def test_report_then_apply(self):
        user, order = self._legacy_cancelled_order()
        balance_before = Balance.objects.get(user=user).amount

        out = StringIO()
        call_command('legacy_refund_report', stdout=out)
        self.assertIn(str(order.pk), out.getvalue())
        self.assertIn('потеряно=0.01', out.getvalue())  # 100 − 30 − 69.99
        self.assertFalse(Op.objects.filter(kind=Op.Kind.REFUND).exists())  # отчёт ничего не меняет

        call_command('legacy_refund_report', '--apply', stdout=StringIO())
        op = Op.objects.get(kind=Op.Kind.REFUND)
        self.assertEqual((op.reason, op.wallet_amount), (Op.Reason.LEGACY_FIX, Decimal('0.01')))
        self.assertEqual(Balance.objects.get(user=user).amount, balance_before + Decimal('0.01'))

        out = StringIO()
        call_command('legacy_refund_report', stdout=out)
        self.assertIn('Итого заказов: 0', out.getvalue())
