import uuid
from decimal import Decimal

from django.db import models
from django.db.models import F
from django.conf import settings
from django.core.validators import MinValueValidator



class Balance(models.Model):
    """Модель баланса пользователя"""
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='balance',
        verbose_name='User'
    )
    amount = models.DecimalField(
        verbose_name='Сумма баланса',
        max_digits=15,
        decimal_places=2,
        default=0.00,
        validators=[MinValueValidator(Decimal('0.00'))]
    )
    add_amount = models.DecimalField(
        verbose_name='Пополнить',
        max_digits=15,
        decimal_places=2,
        null=True,
        blank=True,
        default=0.00,
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text='Сумма будет ДОБАВЛЕНА к балансу'
    )
    # Дробный остаток возвратов, который меньше шага кошелька (AdGram — 1 сум, локально — 0.01).
    # Добавляется к следующему возврату, а при создании заказа целиком уходит в его бюджет.
    # Меняем ТОЛЬКО под select_for_update (см. apps/billing/services.py)
    carry = models.DecimalField(
        verbose_name='Дробный остаток', max_digits=12, decimal_places=6, default=Decimal('0'),
        help_text='Доли сума от возвратов, которые ещё не ушли в кошелёк'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Balance'
        verbose_name_plural = 'Balances'

    def __str__(self):
        return f"{self.user.username}: {self.amount}"

    def deposit(self, amount):
        """
        Пополнение баланса.

        Прибавляем через F-выражение — плюсует сама база данных атомарно.
        Иначе два одновременных зачисления (например, возврат за заказ и
        пополнение админом) могли бы затереть друг друга.
        """
        self.amount = F('amount') + Decimal(amount)
        self.save(update_fields=['amount', 'updated_at'])
        # Перечитываем значение из базы, чтобы в объекте осталось число, а не F-выражение
        self.refresh_from_db(fields=['amount'])

    def withdraw(self, amount):
        """Списание с баланса (с проверкой)"""
        if self.amount >= amount:
            self.amount -= amount
            self.save()
            return True
        return False

    def get_available_amount(self):
        """Получение доступного баланса"""
        return self.amount


class MoneyOperation(models.Model):
    """
    Журнал всех денежных движений — это и есть история операций пользователя.

    id — это operationId для AdGram: повтор запроса с тем же id безопасен,
    деньги второй раз не двигаются. Для локального кошелька операции пишутся
    сразу со статусом DONE (в той же транзакции, что и движение денег).
    """

    class Kind(models.TextChoices):
        DEBIT = 'DEBIT', 'Списание'
        REFUND = 'REFUND', 'Возврат'
        DEPOSIT = 'DEPOSIT', 'Пополнение'

    class Reason(models.TextChoices):
        CREATE = 'CREATE', 'Создание заказа'
        REJECT = 'REJECT', 'Заказ отклонён'
        BLOCK = 'BLOCK', 'Заказ заблокирован'
        CANCEL = 'CANCEL', 'Заказ отменён'
        COMPLETED = 'COMPLETED', 'Остаток завершённого заказа'
        COMPENSATION = 'COMPENSATION', 'Компенсация списания без заказа'
        ADMIN_DEPOSIT = 'ADMIN_DEPOSIT', 'Пополнение суперадмином'
        LEGACY_FIX = 'LEGACY_FIX', 'Возврат старых потерь'
        TOP_UP = 'TOP_UP', 'Пополнение бюджета объявления'
        DELETE = 'DELETE', 'Объявление удалено'
        # Остаток старого заказа переехал в объявление v2 — кошелёк не двигается (wallet_amount = 0)
        MIGRATE = 'MIGRATE', 'Перенос старого заказа в v2'

    class Wallet(models.TextChoices):
        ADGRAM = 'ADGRAM', 'AdGram'
        LOCAL = 'LOCAL', 'Локальный баланс'

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'В процессе'
        DONE = 'DONE', 'Выполнена'
        FAILED = 'FAILED', 'Не прошла'

    class OrderType(models.TextChoices):
        SEARCH_ADS = 'search_ads', 'Реклама в поиске'
        CHAT_ADS = 'chat_ads', 'Реклама в чатах'
        ADS = 'ads', 'Объявление'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # PROTECT: юзера с денежной историей удалить нельзя — история важнее
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                             related_name='money_operations', verbose_name='Пользователь')
    # Заказ храним как тип + id (моделей заказов две). У списания id появляется только
    # после создания заказа; у пополнения заказа нет вообще
    order_type = models.CharField(verbose_name='Тип заказа', max_length=20,
                                  choices=OrderType.choices, blank=True, default='')
    order_id = models.UUIDField(verbose_name='ID заказа', null=True, blank=True)
    kind = models.CharField(verbose_name='Вид', max_length=10, choices=Kind.choices)
    reason = models.CharField(verbose_name='Причина', max_length=20, choices=Reason.choices)
    exact_amount = models.DecimalField(verbose_name='Точная сумма', max_digits=19, decimal_places=6,
                                       help_text='Сколько ушло из заказа / пришло в заказ, без округлений')
    wallet_amount = models.DecimalField(verbose_name='Сумма в кошелёк', max_digits=19, decimal_places=2,
                                        help_text='Сколько реально списано с кошелька / зачислено в него')
    carry_delta = models.DecimalField(verbose_name='Изменение дробного остатка', max_digits=12,
                                      decimal_places=6, default=Decimal('0'))
    wallet = models.CharField(verbose_name='Кошелёк', max_length=10, choices=Wallet.choices)
    status = models.CharField(verbose_name='Статус', max_length=10, choices=Status.choices,
                              default=Status.PENDING)
    attempts = models.PositiveIntegerField(verbose_name='Попыток', default=0)
    last_error = models.TextField(verbose_name='Последняя ошибка', blank=True, default='')
    balance_after = models.DecimalField(verbose_name='Баланс после', max_digits=19, decimal_places=2,
                                        null=True, blank=True)
    # Компенсирующий возврат ссылается на своё списание. OneToOne — база сама не даст
    # вернуть одно списание дважды
    compensates = models.OneToOneField('self', on_delete=models.PROTECT, null=True, blank=True,
                                       related_name='compensation', verbose_name='Компенсирует списание')
    # Когда суперадмину сообщили о проблеме (чтобы не слать уведомление каждую минуту)
    alerted_at = models.DateTimeField(verbose_name='Суперадмин уведомлён', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Денежная операция'
        verbose_name_plural = 'Денежные операции'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'updated_at']),       # добивание PENDING
            models.Index(fields=['kind', 'status', 'order_id']),  # списания без заказа
        ]

    def __str__(self):
        return f'{self.get_kind_display()} {self.wallet_amount} ({self.get_status_display()})'

