import re

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

from apps.billing.models import Balance


User = get_user_model()

# Юзеры AdGram получают username = "ag_" + userId. Регистрация такие имена не пускает
ADGRAM_USERNAME_PREFIX = 'ag_'
# userId у AdGram — cuid (латиница и цифры). Берём с запасом, но без мусора в username
ADGRAM_USER_ID_RE = re.compile(r'[A-Za-z0-9_-]{1,64}')


class AdGramUsernameTaken(Exception):
    """Имя ag_<userId> уже занято другим (старым) пользователем — тень AdGram не создать"""


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


def get_or_create_adgram_user(adgram_user_id, adgram_username=''):
    """
    Пользователь new_dvs («тень») для юзера AdGram — при первом входе или когда админ AdGram
    назначает роль заранее. Рекламодатель с нулевым балансом, без пароля. adgram_user_id уже проверен
    """
    user = User.objects.filter(adgram_user_id=adgram_user_id).first()
    if user is not None:
        return user
    try:
        return create_user_with_balance(username=ADGRAM_USERNAME_PREFIX + adgram_user_id,
                                        adgram_user_id=adgram_user_id, adgram_username=adgram_username)
    except IntegrityError:
        # Два первых запроса пришли одновременно: второй упал на unique — берём того, кого создал первый
        user = User.objects.filter(adgram_user_id=adgram_user_id).first()
        if user is None:
            # unique сработал не на adgram_user_id, а на username — имя уже кем-то занято
            raise AdGramUsernameTaken(adgram_user_id)
        return user
