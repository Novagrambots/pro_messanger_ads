from django.contrib.auth import get_user_model
from django.db import transaction

from apps.billing.models import Balance


User = get_user_model()


def create_user_with_balance(username, password=None, **extra_fields):
    """
    Создаёт пользователя сразу с нулевым балансом (одной транзакцией).

    Используют регистрация и вход через AdGram. password=None — пароль
    "непригодный": войти по логину/паролю таким юзером нельзя (так у юзеров AdGram).
    """
    with transaction.atomic():
        user = User.objects.create_user(username=username, password=password, **extra_fields)
        Balance.objects.create(user=user, amount=0.00)
    return user
