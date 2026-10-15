"""
Клиент внутреннего API AdGram (кошелёк пользователей AdGram).

Все запросы — с заголовком X-Internal-Key. Списание и возврат идемпотентны по
operationId: повтор с тем же id безопасен, деньги второй раз не двигаются.
"""
from dataclasses import dataclass
from decimal import Decimal

import requests
from django.conf import settings


# Таймауты (соединение, ответ) в секундах — дольше пользователя не держим
TIMEOUT = (2, 5)


class AdGramError(Exception):
    """Базовая ошибка общения с AdGram"""


class AdGramNotConfigured(AdGramError):
    """В .env нет ADGRAM_INTERNAL_URL / ADGRAM_INTERNAL_KEY"""


class AdGramUnavailable(AdGramError):
    """Таймаут, обрыв или 5xx — неизвестно, прошла ли операция. Повторяем тем же operationId"""


class AdGramRejected(AdGramError):
    """AdGram ответил отказом (402, 403, 404, 409, прочие 4xx) — операция точно не прошла"""

    def __init__(self, status_code, data):
        self.status_code = status_code
        self.data = data  # тело ответа (dict), например {'balanceUzs': 1000} при 402
        super().__init__(f'AdGram ответил {status_code}: {data}')


@dataclass
class OperationResult:
    status: str             # "done" — выполнено сейчас, "already_done" — было выполнено раньше
    balance_after: Decimal  # баланс в AdGram после операции


@dataclass
class WalletBalance:
    balance_uzs: Decimal
    is_active: bool


def is_configured():
    return bool(settings.ADGRAM_INTERNAL_URL and settings.ADGRAM_INTERNAL_KEY)


def balance(adgram_user_id):
    """Текущий баланс пользователя в AdGram"""
    data = _request('GET', f'balance/{adgram_user_id}')
    if 'balanceUzs' not in data:
        raise AdGramUnavailable(f'Непонятный ответ AdGram на запрос баланса: {data}')
    return WalletBalance(balance_uzs=Decimal(str(data['balanceUzs'])), is_active=bool(data.get('isActive')))


def debit(adgram_user_id, amount, operation_id, note=''):
    """Списать amount сумов (целое число) с кошелька AdGram"""
    return _operation('debit', adgram_user_id, amount, operation_id, note)


def refund(adgram_user_id, amount, operation_id, note=''):
    """Вернуть amount сумов (целое число) в кошелёк AdGram"""
    return _operation('refund', adgram_user_id, amount, operation_id, note)


def _operation(action, adgram_user_id, amount, operation_id, note):
    # AdGram принимает только целые сумы — дробное сюда попасть не должно никогда
    if amount != amount.to_integral_value() or amount <= 0:
        raise ValueError(f'В AdGram можно отправить только целое число сумов > 0, а пришло {amount}')
    data = _request('POST', action, json={
        'userId': adgram_user_id,
        'amount': int(amount),
        'operationId': str(operation_id),
        'note': note,
    })
    # 200, но без ожидаемых полей — не знаем, прошла ли операция. Считаем «недоступен»:
    # повтор тем же operationId безопасен
    if data.get('status') not in ('done', 'already_done') or 'balanceAfter' not in data:
        raise AdGramUnavailable(f'Непонятный ответ AdGram: {data}')
    return OperationResult(status=data['status'], balance_after=Decimal(str(data['balanceAfter'])))


def _request(method, path, json=None):
    if not is_configured():
        raise AdGramNotConfigured('Не заданы ADGRAM_INTERNAL_URL / ADGRAM_INTERNAL_KEY')

    url = f'{settings.ADGRAM_INTERNAL_URL}/api/internal/novagram/{path}'
    try:
        # Редиректы не идём: POST после редиректа превратился бы в GET
        response = requests.request(method, url, json=json, timeout=TIMEOUT, allow_redirects=False,
                                    headers={'X-Internal-Key': settings.ADGRAM_INTERNAL_KEY})
    except requests.RequestException as exc:
        raise AdGramUnavailable(f'AdGram недоступен: {exc}') from exc

    if response.status_code >= 500:
        raise AdGramUnavailable(f'AdGram ответил {response.status_code}')

    try:
        data = response.json()
    except ValueError:
        data = {}

    if response.status_code != 200:
        raise AdGramRejected(response.status_code, data)
    return data
