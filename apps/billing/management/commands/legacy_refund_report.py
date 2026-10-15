from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from apps.billing.models import MoneyOperation
from apps.billing.services import orders_with_leftover, refund
from apps.chat_ads.models import ChatAdOrder
from apps.common.models import ModerationStatus
from apps.search_ads.models import Order


class Command(BaseCommand):
    help = ('Отчёт о деньгах, потерянных старыми закрытыми заказами (отменён / отклонён / заблокирован) '
            'из-за старой формулы возврата. С --apply возвращает их через refund() (reason LEGACY_FIX).')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Вернуть деньги. Без флага — только отчёт, ничего не меняется')

    def handle(self, *args, **options):
        apply = options['apply']
        total = Decimal('0')
        count = 0

        for model in (Order, ChatAdOrder):
            app_label = model._meta.app_label
            # Закрытые заказы, по которым вернули не всё (у живых заказов остаток вернётся сам)
            closed = orders_with_leftover(model).filter(
                Q(cancelled=True) | Q(status__in=[ModerationStatus.REJECTED, ModerationStatus.BLOCKED])
            ).select_related('user').order_by('created_at')

            for order in closed:
                lost = order.unrefunded_amount()
                count += 1
                total += lost
                self.stdout.write(
                    f'{app_label}  {order.pk}  {order.user.username}  '
                    f'бюджет={order.budget}  потрачено={order.spent_amount()}  '
                    f'вернули={order.refunded}  потеряно={lost}'
                )
                if apply:
                    with transaction.atomic():
                        locked = model.objects.select_for_update().get(pk=order.pk)
                        refund(locked, MoneyOperation.Reason.LEGACY_FIX)

        self.stdout.write(f'Итого заказов: {count}, потеряно: {total}')
        if count and not apply:
            self.stdout.write('Это только отчёт. Чтобы вернуть деньги — запустите с --apply')
        elif apply:
            self.stdout.write(self.style.SUCCESS('Возвраты созданы (LEGACY_FIX)'))
