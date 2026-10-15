import logging

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q

from .models import Notification
from .tasks import send_push_notification

logger = logging.getLogger(__name__)


def notify(user, title, body, notif_type, payload):
    """
    Создаёт in-app уведомление и после коммита транзакции ставит
    Celery-задачу отправки push. In-app запись — главное (не теряется),
    push — best-effort поверх неё.
    """
    notification = Notification.objects.create(
        user=user, title=title, body=body, type=notif_type, payload=payload,
    )
    # on_commit — воркер не должен увидеть уведомление раньше, чем оно закоммичено
    # (мы вызываемся из-под transaction.atomic создания/модерации заказа)
    transaction.on_commit(lambda: _enqueue_push(notification.pk))
    return notification


def _enqueue_push(notification_id):
    # .delay() падает, если брокер недоступен (dev без Redis) — уведомление
    # уже в БД, поэтому просто логируем и не роняем ответ юзеру
    try:
        send_push_notification.delay(notification_id)
    except Exception:
        logger.exception('Не удалось поставить push-задачу для уведомления %s', notification_id)


def _notify_moderators(title, body, notif_type, payload):
    """Рассылка всем модераторам и суперадминам"""
    User = get_user_model()
    moderators = User.objects.filter(
        Q(role=User.Role.MODERATOR) | Q(is_superuser=True), is_active=True,
    )
    for moderator in moderators:
        notify(moderator, title, body, notif_type, payload)


def notify_moderators_new_order(order):
    """Заказ создан (PENDING) — сообщаем всем модераторам и суперадминам"""
    ad_type = 'реклама в чатах' if order._meta.app_label == 'chat_ads' else 'реклама в поиске'
    payload = {'order_id': str(order.order_id), 'order_type': order._meta.app_label}
    _notify_moderators('Новый заказ на модерацию', f'«{order.order_name}» ({ad_type}) ждёт проверки',
                       Notification.Type.NEW_ORDER, payload)


# Тексты уведомлений о решении модератора по статусу заказа
_DECISION_TEXTS = {
    'APPROVED': (Notification.Type.ORDER_APPROVED, 'Заказ одобрен',
                 'Ваш заказ «{name}» одобрен и запущен'),
    'REJECTED': (Notification.Type.ORDER_REJECTED, 'Заказ отклонён',
                 'Ваш заказ «{name}» отклонён. Причина: {reason}. Деньги возвращены на баланс'),
    'BLOCKED': (Notification.Type.ORDER_BLOCKED, 'Заказ заблокирован',
                'Ваш заказ «{name}» заблокирован. Причина: {reason}. '
                'Неизрасходованный остаток возвращён на баланс'),
}


def notify_order_decision(order):
    """Решение модератора принято — сообщаем владельцу заказа"""
    notif_type, title, body_template = _DECISION_TEXTS[order.status]
    body = body_template.format(name=order.order_name, reason=order.reject_reason)
    payload = {'order_id': str(order.order_id), 'order_type': order._meta.app_label}
    notify(order.user, title, body, notif_type, payload)


def notify_money_alert(operation, problem):
    """Денежная операция зависла или AdGram ответил ошибкой настройки — сообщаем суперадминам"""
    User = get_user_model()
    body = (f'{operation.get_kind_display()} {operation.wallet_amount} '
            f'({operation.user.username}, операция {operation.pk}): {problem}')
    payload = {'operation_id': str(operation.pk)}
    for admin in User.objects.filter(is_superuser=True, is_active=True):
        notify(admin, 'Проблема с платежом', body[:1000], Notification.Type.MONEY_ALERT, payload)


# ============================== Объявления v2 (apps.ads) ==============================

def _ad_payload(ad, revision=None):
    """Как у старых заказов, только order_type='ads'; у правок ещё revision_id"""
    payload = {'order_id': str(ad.pk), 'order_type': 'ads'}
    if revision is not None:
        payload['revision_id'] = str(revision.pk)
    return payload


def notify_moderators_new_ad(ad):
    """Новое объявление на модерации — модераторам"""
    _notify_moderators('Новый заказ на модерацию',
                       f'Объявление «{ad.title}» ({ad.get_placement_display()}) ждёт проверки',
                       Notification.Type.NEW_ORDER, _ad_payload(ad))


def notify_moderators_new_revision(revision):
    """Новая правка креатива на проверке — модераторам"""
    ad = revision.ad
    _notify_moderators('Правка объявления на проверку',
                       f'В объявлении «{ad.title}» изменили креатив — нужна проверка',
                       Notification.Type.NEW_REVISION, _ad_payload(ad, revision))


# Тексты решений по объявлению v2: действие AdDecision → (тип, заголовок, текст)
_AD_DECISION_TEXTS = {
    'APPROVED': (Notification.Type.ORDER_APPROVED, 'Объявление одобрено',
                 'Ваше объявление «{name}» одобрено'),
    'DECLINED': (Notification.Type.ORDER_REJECTED, 'Объявление отклонено',
                 'Ваше объявление «{name}» отклонено. Причина: {reason}. Деньги возвращены на баланс'),
    'BLOCKED': (Notification.Type.ORDER_BLOCKED, 'Объявление заблокировано',
                'Ваше объявление «{name}» заблокировано. Причина: {reason}. '
                'Неизрасходованный остаток возвращён на баланс'),
    'REVISION_APPROVED': (Notification.Type.REVISION_APPROVED, 'Правка одобрена',
                          'Правка объявления «{name}» одобрена и уже показывается'),
    'REVISION_DECLINED': (Notification.Type.REVISION_DECLINED, 'Правка отклонена',
                          'Правка объявления «{name}» отклонена. Причина: {reason}. '
                          'Объявление показывается в прежнем виде'),
}


def notify_ad_decision(ad, action, reason='', revision=None):
    """Решение модератора по объявлению или его правке — владельцу"""
    notif_type, title, body_template = _AD_DECISION_TEXTS[action]
    body = body_template.format(name=ad.title, reason=reason)
    notify(ad.user, title, body[:1000], notif_type, _ad_payload(ad, revision))

