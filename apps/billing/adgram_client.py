"""
Клиент внутреннего API AdGram: кошелёк пользователей AdGram и данные ads.telegram.org
(поиск канала/бота и похожие).

Все запросы — с заголовком X-Internal-Key. Списание и возврат идемпотентны по
operationId: повтор с тем же id безопасен, деньги второй раз не двигаются.
"""
from dataclasses import dataclass
from decimal import Decimal

import requests
from django.conf import settings


# Таймауты (соединение, ответ) в секундах — дольше пользователя не держим
TIMEOUT = (2, 5)
# Для данных Telegram: у AdGram нет своего таймаута на запрос к Telegram — наш обязателен
TG_TIMEOUT = (2, 10)


class AdGramError(Exception):
    """Базовая ошибка общения с AdGram"""


class AdGramNotConfigured(AdGramError):
    """В .env нет ADGRAM_INTERNAL_URL / ADGRAM_INTERNAL_KEY"""


class AdGramUnavailable(AdGramError):
    """Таймаут, обрыв или 5xx — неизвестно, прошла ли операция. Повторяем тем же operationId"""

    def __init__(self, message='', retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after  # секунды из Retry-After (у 429 данных Telegram)


class AdGramRejected(AdGramError):
    """AdGram ответил отказом (402, 403, 404, 409, прочие 4xx) — операция точно не прошла"""

    def __init__(self, status_code, data, headers=None):
        self.status_code = status_code
        self.data = data  # тело ответа (dict), например {'balanceUzs': 1000} при 402
        self.headers = headers or {}
        super().__init__(f'AdGram ответил {status_code}: {data}')


@dataclass
class OperationResult:
    status: str             # "done" — выполнено сейчас, "already_done" — было выполнено раньше
    balance_after: Decimal  # баланс в AdGram после операции


@dataclass
class WalletBalance:
    balance_uzs: Decimal
    is_active: bool


@dataclass
class TgPeer:
    """Канал или бот с ads.telegram.org"""
    ads_ref: str            # непрозрачный хэш ads.telegram.org — НЕ числовой id Telegram
    username: str | None
    title: str
    photo_url: str | None   # https://cdn<цифры>.telesco.pe/... или None
    subscribers: int | None = None
    monthly_users: int | None = None


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


# ============================ Данные ads.telegram.org (похожие) ============================

def search_channel(username, timeout=TG_TIMEOUT):
    """Канал на ads.telegram.org по имени; None — не найден"""
    return _tg_search('search-channel', username, timeout)


def search_bot(username, timeout=TG_TIMEOUT):
    """Бот на ads.telegram.org по имени; None — не найден"""
    return _tg_search('search-bot', username, timeout)


def similar_channels(ads_refs, timeout=TG_TIMEOUT):
    """Похожие каналы для 1–10 каналов (по их ads_ref)"""
    data = _tg_request('similar-channels', {'channelIds': list(ads_refs)}, timeout)
    return [_tg_peer(item) for item in _tg_list(data, 'channels')]


def similar_bots(ads_refs, timeout=TG_TIMEOUT):
    """Похожие боты для 1–9 ботов (по их ads_ref)"""
    data = _tg_request('similar-bots', {'botIds': list(ads_refs)}, timeout)
    return [_tg_peer(item) for item in _tg_list(data, 'bots')]


def _tg_search(path, username, timeout):
    try:
        return _tg_peer(_tg_request(path, {'username': username}, timeout))
    except AdGramRejected as exc:
        if exc.status_code == 404:
            return None
        raise


def _tg_request(path, payload, timeout):
    """
    429 (лимит Telegram) и 503 (Telegram недоступен) → AdGramUnavailable — повторим позже.
    У денежных функций 429 по-прежнему отказ — их поведение не меняем.
    """
    try:
        return _request('POST', f'tg/{path}', json=payload, timeout=timeout)
    except AdGramRejected as exc:
        if exc.status_code == 429:
            raise AdGramUnavailable('AdGram: лимит запросов к Telegram (429)',
                                    retry_after=_retry_after(exc.headers)) from None
        raise


def _retry_after(headers):
    try:
        return max(int(headers.get('Retry-After', '')), 0)
    except (TypeError, ValueError):
        return None


def _tg_list(data, key):
    items = data.get(key)
    if not isinstance(items, list):
        raise AdGramUnavailable(f'Непонятный ответ AdGram: нет списка {key}')
    return items


def _tg_peer(item):
    if not isinstance(item, dict) or not item.get('id'):
        raise AdGramUnavailable('Непонятный ответ AdGram: нет id')

    def number(key):
        value = item.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    return TgPeer(ads_ref=str(item['id']), username=item.get('username') or None, title=item.get('title') or '',
                  photo_url=item.get('photoUrl') or None, subscribers=number('subscribers'),
                  monthly_users=number('monthlyUsers'))


def _request(method, path, json=None, timeout=TIMEOUT):
    if not is_configured():
        raise AdGramNotConfigured('Не заданы ADGRAM_INTERNAL_URL / ADGRAM_INTERNAL_KEY')

    url = f'{settings.ADGRAM_INTERNAL_URL}/api/internal/novagram/{path}'
    try:
        # Редиректы не идём: POST после редиректа превратился бы в GET
        response = requests.request(method, url, json=json, timeout=timeout, allow_redirects=False,
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
        raise AdGramRejected(response.status_code, data, response.headers)
    return data
