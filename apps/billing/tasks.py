from core.celery import app

from apps.billing import services
from apps.billing.adgram_client import AdGramUnavailable


@app.task(bind=True, max_retries=5, default_retry_delay=30)
def send_money_operation(self, operation_id):
    """Отправка операции (возврата) в AdGram. Не вышло — повтор через 30 с, дальше подхватит минутный beat"""
    try:
        services.send_operation(operation_id)
    except AdGramUnavailable as exc:
        raise self.retry(exc=exc)


@app.task
def process_money_operations():
    """Раз в минуту: добить PENDING, вернуть списания без заказа, предупредить о зависших"""
    services.retry_pending_operations()
    services.compensate_orphan_debits()
    services.alert_stuck_operations()


@app.task
def refund_completed_orders():
    """Раз в 5 минут: вернуть дробный остаток бюджета завершённых заказов"""
    services.refund_completed_orders()
