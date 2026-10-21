"""
Деньги заказов: одна развилка «кошелёк AdGram или локальный баланс».

Главное правило — пользователь не теряет ни доли сума:
- всё считаем в Decimal точно, без float и без усечений;
- в кошелёк уходит сумма, округлённая ВНИЗ до шага кошелька (AdGram — 1 сум, локально — 0.01),
  а дробная часть копится в Balance.carry: уходит со следующим возвратом или в бюджет нового заказа;
- каждое движение денег пишется в журнал MoneyOperation (это и есть история операций).
"""
import logging
from datetime import timedelta
from decimal import Decimal, ROUND_DOWN

from django.db import IntegrityError, transaction
from django.db.models import DecimalField, ExpressionWrapper, F, Q
from django.utils import timezone
from rest_framework import exceptions, serializers

from apps.billing import adgram_client
from apps.billing.adgram_client import AdGramError, AdGramNotSent, AdGramRejected, AdGramUnavailable
from apps.billing.models import Balance, MoneyOperation
from apps.common.models import ModerationStatus

logger = logging.getLogger(__name__)

# Шаг кошелька: в AdGram переводим целые сумы, в локальный баланс — копейки
WALLET_STEP = {
    MoneyOperation.Wallet.ADGRAM: Decimal('1'),
    MoneyOperation.Wallet.LOCAL: Decimal('0.01'),
}

# Новая строка баланса — явно в Decimal: дефолт модели 0.00 — это float,
# и арифметика с Decimal на только что созданной строке упала бы
_NEW_BALANCE = {'amount': Decimal('0'), 'carry': Decimal('0')}

RETRY_AFTER = timedelta(minutes=1)        # PENDING без движения дольше — добиваем повтором
COMPENSATE_AFTER = timedelta(minutes=10)  # списание без заказа дольше — возвращаем
ALERT_AFTER = timedelta(hours=1)          # PENDING дольше — зовём суперадмина
ALERT_ATTEMPTS = 20                       # ...или если попыток больше


class PaymentUnavailable(exceptions.APIException):
    status_code = 503
    default_detail = 'Платёж не подтверждён, попробуйте позже'
    default_code = 'payment_unavailable'


def wallet_of(user):
    """У юзера AdGram кошелёк — баланс AdGram, у остальных — локальный Balance"""
    return MoneyOperation.Wallet.ADGRAM if user.adgram_user_id else MoneyOperation.Wallet.LOCAL


def format_money(amount):
    """Сумма строкой для ответа: минимум 2 знака, лишние нули справа убираем (точность не теряем)"""
    if amount == amount.quantize(Decimal('0.01')):
        return str(amount.quantize(Decimal('0.01')))
    return str(amount.normalize())


def check_budget(user, budget):
    """Бюджет с клиента: не больше 2 знаков после запятой, у юзера AdGram — только целые сумы"""
    if budget != budget.quantize(Decimal('0.01')):
        raise serializers.ValidationError('Бюджет — не больше 2 знаков после запятой')
    if user.adgram_user_id and budget != budget.to_integral_value():
        raise serializers.ValidationError('При оплате через AdGram бюджет должен быть целым числом сумов')
    return budget


# ======================================= Списание при создании заказа =======================================

def charge(user, paid, order_type, create_order, reason=MoneyOperation.Reason.CREATE, lock_order=None):
    """
    Списывает paid. create_order(budget) вызывается внутри нашей транзакции и возвращает
    заказ: создаёт новый (CREATE) или увеличивает бюджет существующего под select_for_update
    (TOP_UP). В бюджет уходит оплачено + carry (carry обнуляется).

    lock_order() — у пополнения существующего заказа: лочит его в начале транзакции, ДО баланса.
    Порядок блокировок везде один — заказ, потом баланс (так же идёт возврат), иначе
    одновременные пополнение и удаление одного заказа ловят взаимную блокировку.

    Вызывать ВНЕ transaction.atomic: у AdGram запись PENDING должна закоммититься
    до похода в AdGram, а сам поход идёт без открытой транзакции БД.
    """
    if user.adgram_user_id:
        return _charge_adgram(user, paid, order_type, create_order, reason, lock_order)
    return _charge_local(user, paid, order_type, create_order, reason, lock_order)


def _charge_local(user, paid, order_type, create_order, reason, lock_order):
    with transaction.atomic():
        if lock_order:
            lock_order()
        balance = _lock_balance(user)
        if balance.amount < paid:
            # Списком, как ошибки валидации DRF: эта ошибка рождается уже в save(), и DRF её сам не обернёт
            raise serializers.ValidationError({'budget': [f'Недостаточно средств. Доступно: {balance.amount}']})

        carry = balance.carry
        balance.amount -= paid
        balance.carry = Decimal('0')
        balance.save(update_fields=['amount', 'carry', 'updated_at'])

        order = create_order(paid + carry)
        MoneyOperation.objects.create(
            user=user, order_type=order_type, order_id=order.pk,
            kind=MoneyOperation.Kind.DEBIT, reason=reason,
            wallet=MoneyOperation.Wallet.LOCAL, status=MoneyOperation.Status.DONE,
            exact_amount=paid, wallet_amount=paid, carry_delta=-carry, balance_after=balance.amount,
        )
    return order


def _charge_adgram(user, paid, order_type, create_order, reason, lock_order):
    if not adgram_client.is_configured():
        raise PaymentUnavailable('Оплата через AdGram временно недоступна, попробуйте позже')

    # a) Фиксируем намерение списать и сразу коммитим — запись переживёт даже падение процесса
    op = MoneyOperation.objects.create(
        user=user, order_type=order_type,
        kind=MoneyOperation.Kind.DEBIT, reason=reason,
        wallet=MoneyOperation.Wallet.ADGRAM, status=MoneyOperation.Status.PENDING,
        exact_amount=paid, wallet_amount=paid, attempts=1,
    )

    # b) Идём в AdGram вне транзакции БД
    try:
        result = adgram_client.debit(user.adgram_user_id, paid, op.pk, note=_note(op))
    except AdGramNotSent as exc:
        # Запрос точно не дошёл до AdGram — деньги не списаны: сразу FAILED, без добивания и компенсации
        MoneyOperation.objects.filter(pk=op.pk).update(
            status=MoneyOperation.Status.FAILED, last_error=str(exc)[:2000], updated_at=timezone.now())
        raise PaymentUnavailable('AdGram сейчас недоступен — деньги не списаны, попробуйте позже')
    except AdGramUnavailable as exc:
        _save_error(op, exc)  # остаётся PENDING — beat повторит тем же operationId
        raise PaymentUnavailable()
    except AdGramRejected as exc:
        _fail_debit(op, exc)
        raise _debit_error_for_client(user, exc)

    # c) Деньги списаны — одной транзакцией создаём заказ и закрываем операцию
    try:
        with transaction.atomic():
            if lock_order:
                lock_order()
            balance = _lock_balance(user)
            carry = balance.carry
            balance.carry = Decimal('0')
            balance.save(update_fields=['carry', 'updated_at'])

            order = create_order(paid + carry)
            MoneyOperation.objects.filter(pk=op.pk).update(
                status=MoneyOperation.Status.DONE, order_id=order.pk, carry_delta=-carry,
                balance_after=result.balance_after, last_error='', updated_at=timezone.now(),
            )
    except Exception as exc:
        # Деньги ушли, а заказ не создался: списание DONE без заказа — beat вернёт его компенсацией
        MoneyOperation.objects.filter(pk=op.pk).update(
            status=MoneyOperation.Status.DONE, balance_after=result.balance_after,
            last_error=f'Заказ не создан: {exc}'[:2000], updated_at=timezone.now(),
        )
        raise
    return order


def _debit_error_for_client(user, exc):
    """Отказ AdGram в списании → понятный ответ пользователю"""
    # Ошибки по полю — списком, как у DRF; balance — значение, а не ошибка
    if exc.status_code == 402:
        detail = {'budget': ['Недостаточно средств на балансе AdGram']}
        if exc.data.get('balanceUzs') is not None:
            # Как в /api/balance/: баланс AdGram + дробный остаток у нас
            detail['balance'] = format_money(Decimal(str(exc.data['balanceUzs'])) + _carry_of(user))
        return serializers.ValidationError(detail)
    if exc.status_code == 403:
        return exceptions.PermissionDenied('Пользователь заблокирован в AdGram')
    if exc.status_code == 404:
        return serializers.ValidationError({'budget': ['Пользователь не найден в AdGram']})
    return PaymentUnavailable()


def _fail_debit(op, exc):
    """AdGram отказал в списании — деньги точно не ушли, операция FAILED"""
    MoneyOperation.objects.filter(pk=op.pk, status=MoneyOperation.Status.PENDING).update(
        status=MoneyOperation.Status.FAILED, last_error=str(exc)[:2000], updated_at=timezone.now(),
    )
    # 402/403/404 — обычные отказы. Остальное (400, 401, 409...) — ошибка настройки или баг
    if exc.status_code not in (402, 403, 404):
        _alert(op, f'AdGram отказал в списании: {exc}')


# ========================================== Возврат по заказу ==========================================

def refund(order, reason):
    """
    Возвращает весь невозвращённый остаток заказа: budget − потрачено − уже возвращено.
    Повторный вызов вернёт 0. Возвращает точную сумму, ушедшую из заказа.

    Вызывать ВНУТРИ transaction.atomic, на заказе, взятом через select_for_update, —
    иначе два параллельных события вернут деньги дважды.
    """
    exact = order.unrefunded_amount()
    if exact <= 0:
        return Decimal('0')

    order.refunded += exact
    order.save(update_fields=['refunded', 'updated_at'])

    user = order.user
    wallet = wallet_of(user)
    balance = _lock_balance(user)
    # В кошелёк — округлённое вниз до шага, дробная часть остаётся в carry
    total = exact + balance.carry
    wallet_amount = total.quantize(WALLET_STEP[wallet], rounding=ROUND_DOWN)
    new_carry = total - wallet_amount

    op = MoneyOperation(
        user=user, order_type=order._meta.app_label, order_id=order.pk,
        kind=MoneyOperation.Kind.REFUND, reason=reason, wallet=wallet,
        exact_amount=exact, wallet_amount=wallet_amount, carry_delta=new_carry - balance.carry,
    )
    balance.carry = new_carry

    if wallet == MoneyOperation.Wallet.LOCAL:
        balance.amount += wallet_amount
        balance.save(update_fields=['amount', 'carry', 'updated_at'])
        op.status = MoneyOperation.Status.DONE
        op.balance_after = balance.amount
    else:
        balance.save(update_fields=['carry', 'updated_at'])
        # Всё легло в carry — слать в AdGram нечего
        op.status = MoneyOperation.Status.PENDING if wallet_amount > 0 else MoneyOperation.Status.DONE
    op.save()

    if op.status == MoneyOperation.Status.PENDING:
        # В AdGram — только после коммита: воркер не должен увидеть незакоммиченную операцию
        transaction.on_commit(lambda: enqueue_send(op.pk))
    return exact


def refund_wallet_amount(order, reason, exact):
    """
    Сколько возврат refund(order, reason) реально зачислил в кошелёк — для ответа фронту (тост о возврате):
    refund отдаёт точную сумму, а в кошелёк уходит округлённое вниз вместе с прошлым carry.
    exact — что вернул refund; 0 — операции не было (не путаем с прошлым возвратом той же причины)
    """
    if not exact:
        return Decimal('0')
    op = (MoneyOperation.objects.filter(order_type=order._meta.app_label, order_id=order.pk,
                                        kind=MoneyOperation.Kind.REFUND, reason=reason)
          .order_by('-created_at').first())
    return op.wallet_amount


def orders_with_leftover(model):
    """Заказы, по которым вернули не всё: budget − показано×spm/1000 − refunded > 0 (сравниваем без деления)"""
    leftover = ExpressionWrapper(
        F('budget') * 1000 - F('shown_views') * F('spm') - F('refunded') * 1000,
        output_field=DecimalField(max_digits=30, decimal_places=6),
    )
    return model.objects.annotate(leftover_x1000=leftover).filter(leftover_x1000__gt=0)


def refund_completed_orders():
    """Возвращает дробный остаток бюджета завершённых заказов (раньше терялся из-за int() в total_views)"""
    from apps.chat_ads.models import ChatAdOrder
    from apps.search_ads.models import Order

    for model in (Order, ChatAdOrder):
        pks = orders_with_leftover(model).filter(
            completed=True, cancelled=False, status=ModerationStatus.APPROVED,
        ).values_list('pk', flat=True)
        for pk in pks:
            with transaction.atomic():
                order = model.objects.select_for_update().get(pk=pk)
                # Перепроверяем под локом — пока мы шли по списку, статус мог смениться
                if order.completed and not order.cancelled and order.status == ModerationStatus.APPROVED:
                    refund(order, MoneyOperation.Reason.COMPLETED)


# ===================================== Отправка в AdGram и добивание =====================================

def enqueue_send(operation_id):
    """Ставит отправку операции в Celery. Брокер недоступен — не страшно, операцию добьёт beat"""
    from apps.billing.tasks import send_money_operation
    try:
        send_money_operation.delay(str(operation_id))
    except Exception:
        logger.exception('Не удалось поставить отправку денежной операции %s', operation_id)


def send_operation(operation_id, stale_before=None):
    """
    Отправляет PENDING-операцию в AdGram тем же operationId (повтор безопасен).
    stale_before — берём операцию, только если её не трогали с этого момента:
    так beat и Celery-задача не шлют одно и то же одновременно.
    Бросает AdGramUnavailable, чтобы Celery-задача повторила попытку.
    """
    if not adgram_client.is_configured():
        return  # настроек нет — копим PENDING до их появления

    # «Захват» операции: +1 попытка, только если она всё ещё PENDING
    claim = MoneyOperation.objects.filter(
        pk=operation_id, status=MoneyOperation.Status.PENDING, wallet=MoneyOperation.Wallet.ADGRAM,
    )
    if stale_before is not None:
        claim = claim.filter(updated_at__lt=stale_before)
    if not claim.update(attempts=F('attempts') + 1, updated_at=timezone.now()):
        return  # уже выполнена или её прямо сейчас шлёт кто-то другой

    op = MoneyOperation.objects.select_related('user').get(pk=operation_id)
    send = adgram_client.debit if op.kind == MoneyOperation.Kind.DEBIT else adgram_client.refund
    try:
        result = send(op.user.adgram_user_id, op.wallet_amount, op.pk, note=_note(op))
    except AdGramUnavailable as exc:
        _save_error(op, exc)
        raise
    except AdGramRejected as exc:
        if op.kind == MoneyOperation.Kind.DEBIT:
            _fail_debit(op, exc)
        else:
            # Возврат не сдаётся никогда: остаётся PENDING, суперадмину — уведомление
            _save_error(op, exc)
            _alert(op, f'AdGram отказал в возврате: {exc}')
        return

    MoneyOperation.objects.filter(pk=op.pk, status=MoneyOperation.Status.PENDING).update(
        status=MoneyOperation.Status.DONE, balance_after=result.balance_after,
        last_error='', updated_at=timezone.now(),
    )


def retry_pending_operations():
    """Добивает PENDING (и списания, и возвраты), которые не трогали дольше минуты"""
    stale_before = timezone.now() - RETRY_AFTER
    pks = MoneyOperation.objects.filter(
        status=MoneyOperation.Status.PENDING, wallet=MoneyOperation.Wallet.ADGRAM,
        updated_at__lt=stale_before,
    ).order_by('created_at').values_list('pk', flat=True)
    for pk in pks:
        try:
            send_operation(pk, stale_before=stale_before)
        except AdGramUnavailable:
            break  # AdGram лежит — остальные попробуем через минуту, не ждём таймаут на каждой


def compensate_orphan_debits():
    """Списание прошло, а заказа нет дольше 10 минут — возвращаем деньги (ровно один раз)"""
    orphans = MoneyOperation.objects.filter(
        kind=MoneyOperation.Kind.DEBIT, wallet=MoneyOperation.Wallet.ADGRAM,
        status=MoneyOperation.Status.DONE, order_id__isnull=True,
        created_at__lt=timezone.now() - COMPENSATE_AFTER, compensation__isnull=True,
    )
    for debit in orphans:
        try:
            with transaction.atomic():
                compensation = MoneyOperation.objects.create(
                    user_id=debit.user_id, order_type=debit.order_type,
                    kind=MoneyOperation.Kind.REFUND, reason=MoneyOperation.Reason.COMPENSATION,
                    wallet=MoneyOperation.Wallet.ADGRAM, status=MoneyOperation.Status.PENDING,
                    exact_amount=debit.wallet_amount, wallet_amount=debit.wallet_amount,
                    compensates=debit,
                )
        except IntegrityError:
            continue  # компенсацию по этому списанию уже создали параллельно
        enqueue_send(compensation.pk)


def alert_stuck_operations():
    """PENDING дольше часа или больше 20 попыток — сообщаем суперадминам (один раз)"""
    stuck = MoneyOperation.objects.filter(
        status=MoneyOperation.Status.PENDING, alerted_at__isnull=True,
    ).filter(
        Q(created_at__lt=timezone.now() - ALERT_AFTER) | Q(attempts__gt=ALERT_ATTEMPTS)
    ).select_related('user')
    for op in stuck:
        _alert(op, f'операция зависла в статусе PENDING (попыток: {op.attempts})')


# ========================================== Баланс и пополнение ==========================================

def wallet_amount_for_display(user):
    """Баланс для /api/balance/: у юзера AdGram — баланс AdGram + carry, у остальных — локальный"""
    balance, _ = Balance.objects.get_or_create(user=user, defaults=_NEW_BALANCE)
    if not user.adgram_user_id:
        return balance.amount
    try:
        info = adgram_client.balance(user.adgram_user_id)
    except AdGramError:
        raise PaymentUnavailable('Баланс AdGram сейчас недоступен, попробуйте позже')
    return info.balance_uzs + balance.carry


def admin_deposit(user, amount):
    """Пополнение локального баланса суперадмином + запись в журнал. Возвращает баланс"""
    with transaction.atomic():
        balance = _lock_balance(user)
        balance.amount += amount
        balance.save(update_fields=['amount', 'updated_at'])
        log_admin_deposit(user, amount, balance.amount)
    return balance


def log_admin_deposit(user, amount, balance_after):
    """Запись пополнения суперадмином в журнал (вызывать в той же транзакции, что и пополнение)"""
    MoneyOperation.objects.create(
        user=user, kind=MoneyOperation.Kind.DEPOSIT, reason=MoneyOperation.Reason.ADMIN_DEPOSIT,
        wallet=MoneyOperation.Wallet.LOCAL, status=MoneyOperation.Status.DONE,
        exact_amount=amount, wallet_amount=amount, balance_after=balance_after,
    )


# ================================================ Мелочи ================================================

def _lock_balance(user):
    """Строка баланса под select_for_update (если её нет — создаём, например у суперадмина)"""
    balance, _ = Balance.objects.select_for_update().get_or_create(user=user, defaults=_NEW_BALANCE)
    return balance


def _carry_of(user):
    carry = Balance.objects.filter(user=user).values_list('carry', flat=True).first()
    return carry or Decimal('0')


def _save_error(op, exc):
    MoneyOperation.objects.filter(pk=op.pk).update(last_error=str(exc)[:2000], updated_at=timezone.now())


def _alert(op, problem):
    """Сообщаем суперадминам о проблеме с операцией — один раз на операцию"""
    if MoneyOperation.objects.filter(pk=op.pk, alerted_at__isnull=True).update(alerted_at=timezone.now()):
        logger.error('Проблема с денежной операцией %s: %s', op.pk, problem)
        from apps.notifications.services import notify_money_alert
        notify_money_alert(op, problem)


def _note(op):
    """Комментарий к операции для AdGram (видно в их истории)"""
    note = f'NovaGram: {op.get_reason_display()}'
    if op.order_id:
        note += f', заказ {op.order_id}'
    if op.order_type:
        note += f' ({op.get_order_type_display()})'
    return note
