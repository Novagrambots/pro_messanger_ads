"""
Мониторинг трафика выдачи (DEV-77): в ноябре трафик iOS-сборки 89 на проде упал до нуля, и этого никто
не заметил. Счётчики пишет горячий путь (apps/ads/serving/monitor.py), здесь — всё остальное.

Раз в час (задача monitor_serve_traffic, минута 5):
1. save_hours — хеши прошлых часов из Redis выдачи → ServeTrafficHour (повтор безопасен: upsert);
2. check_drops — у кого из клиентов (платформа · версия · User-Agent) трафик за прошлый час резко упал
   против обычного → алерт в Telegram; вернулся — сообщение «tiklandi». Об одном и том же падении
   напоминаем не чаще раза в сутки (отметка в Redis выдачи).
Раз в сутки (report_serve_traffic, 09:10) — отчёт за вчера: запросы и fill rate по клиентам.
Тексты сообщений — на узбекском: их читает команда.
"""
import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

import redis
import requests
from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from apps.ads.models import PLATFORM_NAMES, ServeTrafficHour, client_label
from apps.ads.serving import monitor
from apps.ads.serving import store as S

logger = logging.getLogger(__name__)

SAVE_HOURS = 6          # сколько прошлых часов перечитывать из Redis — догнать, если воркер лежал
ALERT_TTL = 24 * 3600   # напоминание об одном и том же падении — не чаще раза в сутки
REPORT_TOP = 12         # клиентов в суточном отчёте (остальные — одной строкой)
TELEGRAM_LIMIT = 4000   # у Telegram не больше 4096 символов в сообщении
UNIQUE = ['hour', 'platform', 'app_version', 'user_agent']


def alert_key(client):
    """Отметка «о падении клиента уже сообщили» (живёт сутки)"""
    return f'{S.PREFIX}mon:alert:{monitor.SEP.join(client)}'


def hour_start(moment):
    """Начало часа по Ташкенту (aware)"""
    return moment.astimezone(S.tz()).replace(minute=0, second=0, microsecond=0)


def last_hour(now=None):
    """Последний закончившийся час"""
    return hour_start(now or timezone.now()) - timedelta(hours=1)


# ======================================= Redis → Postgres =======================================

def save_hours(last, hours=SAVE_HOURS):
    """Часы last, last − 1 ч, … из Redis выдачи → ServeTrafficHour. Возвращает число строк"""
    rows = []
    for back in range(hours):
        hour = last - timedelta(hours=back)
        for (platform, version, product), (count, filled) in monitor.read_hour(monitor.hour_of(hour)).items():
            rows.append(ServeTrafficHour(hour=hour, platform=platform, app_version=version, user_agent=product,
                                         requests=count, filled=min(filled, count)))
    if rows:
        ServeTrafficHour.objects.bulk_create(rows, update_conflicts=True, unique_fields=UNIQUE,
                                             update_fields=['requests', 'filled'])
    return len(rows)


def counts(hour):
    """{клиент: (запросы, с объявлением)} за час из Postgres"""
    return {row.client: (row.requests, row.filled) for row in ServeTrafficHour.objects.filter(hour=hour)}


# ========================================= Падения =========================================

@dataclass
class Check:
    """Итог проверки часа. В списках — (клиент, запросы за час, обычно, с объявлением)"""
    hour: datetime
    dropped: list = field(default_factory=list)
    recovered: list = field(default_factory=list)
    platforms: dict = field(default_factory=dict)  # платформа → (запросы за час, обычно)


def usual_of(history, key):
    """Обычный трафик — медиана того же часа за прошлые сутки (нет строки — 0: новая сборка ещё не обычная)"""
    return statistics.median(day.get(key, 0) for day in history)


def check_drops(hour, send=True):
    """
    Трафик клиентов за час hour против обычного. Проверяем только клиентов с обычным трафиком
    от MONITOR_MIN_HOURLY в час; упал ниже MONITOR_DROP_RATIO от обычного — падение.
    send=False — только посчитать: без Telegram и без отметок в Redis
    """
    current = counts(hour)
    history = [counts(hour - timedelta(days=day)) for day in range(1, settings.MONITOR_BASELINE_DAYS + 1)]
    requests_now = {client: values[0] for client, values in current.items()}
    requests_then = [{client: values[0] for client, values in day.items()} for day in history]
    redis_client = S.store().redis
    check = Check(hour)
    for client in sorted(set(requests_now).union(*requests_then)):
        usual = usual_of(requests_then, client)
        if usual < settings.MONITOR_MIN_HOURLY:
            continue
        count, filled = current.get(client, (0, 0))
        item = (client, count, usual, filled)
        alerted = redis_client.exists(alert_key(client))
        if count < usual * settings.MONITOR_DROP_RATIO:
            if not alerted:
                check.dropped.append(item)
        elif alerted:
            check.recovered.append(item)

    # Итог по платформам — понять, падение это сборки (обновились на новую) или всей платформы
    def by_platform(day):
        totals = {}
        for (platform, _, _), value in day.items():
            totals[platform] = totals.get(platform, 0) + value
        return totals
    platforms_now, platforms_then = by_platform(requests_now), [by_platform(day) for day in requests_then]
    for platform in {item[0][0] for item in check.dropped + check.recovered}:
        check.platforms[platform] = (platforms_now.get(platform, 0), usual_of(platforms_then, platform))

    if send and check.dropped and send_telegram(drop_message(check)):
        pipe = redis_client.pipeline(transaction=False)
        for client, *_ in check.dropped:
            pipe.set(alert_key(client), 1, ex=ALERT_TTL)
        pipe.execute()
    if send and check.recovered and send_telegram(recovered_message(check)):
        redis_client.delete(*(alert_key(client) for client, *_ in check.recovered))
    return check


def run_hourly(now=None):
    """Задача раз в час: прошлые часы в Postgres и проверка последнего закончившегося часа"""
    hour = last_hour(now)
    try:
        save_hours(hour)
        return check_drops(hour)
    except redis.RedisError:
        # Без Redis выдачи нет и самой выдачи — об этом и сообщаем (раз в час, пока не починят)
        logger.warning('Мониторинг выдачи: Redis выдачи недоступен')
        send_telegram(f'⚠️ [{settings.MONITOR_LABEL}] Reklama monitoringi: Redis выдачи (redis_ads) javob '
                      f'bermayapti — {period(hour)}. Serve ham ishlamayotgan bo\'lishi mumkin.')
        return None


# ========================================= Тексты =========================================

def number(value):
    """12345 → «12 345»"""
    return f'{int(round(value)):,}'.replace(',', ' ')


def fill(filled, count):
    return f'{filled / count:.0%}' if count else '—'


def change(now, before):
    """Изменение против «до»: «+12%», «-100%»; до было 0 — «yangi»"""
    return f'{now / before - 1:+.0%}' if before else 'yangi'


def period(hour):
    local = hour.astimezone(S.tz())
    return f'{local:%d.%m %H}:00–{local + timedelta(hours=1):%H}:00'


def _platform_lines(check):
    return [f'{PLATFORM_NAMES.get(platform, "?")} jami: {number(count)} (odatda ~{number(usual)})'
            for platform, (count, usual) in sorted(check.platforms.items())]


def drop_message(check, title='Reklama trafigi keskin tushdi'):
    lines = [f'⚠️ [{settings.MONITOR_LABEL}] {title} — {period(check.hour)}']
    for client, count, usual, filled in check.dropped:
        lines.append(f'• {client_label(client)}: {number(count)} ta serve (odatda ~{number(usual)}, '
                     f'{change(count, usual)}), fill {fill(filled, count)}')
    lines += _platform_lines(check)
    lines.append(f'«Odatda» — oxirgi {settings.MONITOR_BASELINE_DAYS} kunda shu soatdagi mediana.')
    return '\n'.join(lines)


def recovered_message(check):
    lines = [f'✅ [{settings.MONITOR_LABEL}] Reklama trafigi tiklandi — {period(check.hour)}']
    for client, count, usual, filled in check.recovered:
        lines.append(f'• {client_label(client)}: {number(count)} ta serve (odatda ~{number(usual)}), '
                     f'fill {fill(filled, count)}')
    lines += _platform_lines(check)
    return '\n'.join(lines)


def test_message():
    """Пример алерта для проверки бота и чата (python manage.py serve_traffic --test)"""
    check = Check(last_hour(), dropped=[(('ios', '-', 'Novagram/89'), 0, 5230, 0),
                                        (('android', '1.4.0', 'okhttp/3.14.9'), 820, 3100, 336)],
                  platforms={'ios': (1400, 6800), 'android': (2900, 5200)})
    return drop_message(check, title='SINOV xabari (haqiqiy emas): reklama trafigi keskin tushdi')


# ======================================= Отчёт за сутки =======================================

def day_totals(day):
    """{клиент: (запросы, с объявлением)} за сутки day (date, по Ташкенту)"""
    start = datetime.combine(day, time.min, tzinfo=S.tz())
    rows = (ServeTrafficHour.objects.filter(hour__gte=start, hour__lt=start + timedelta(days=1))
            .values('platform', 'app_version', 'user_agent').annotate(n=Sum('requests'), f=Sum('filled')))
    return {(row['platform'], row['app_version'], row['user_agent']): (row['n'], row['f']) for row in rows}


def daily_report(day=None, send=True):
    """Отчёт за сутки (по умолчанию — вчера): запросы и fill rate по клиентам, изменение против позавчера"""
    day = day or timezone.now().astimezone(S.tz()).date() - timedelta(days=1)
    today, before = day_totals(day), day_totals(day - timedelta(days=1))
    lines = [f'📊 [{settings.MONITOR_LABEL}] Reklama trafigi — {day:%d.%m.%Y}']
    if not today:
        lines.append('Ma\'lumot yo\'q: serve so\'rovlari hisoblanmagan.')
    else:
        total, total_filled = sum(n for n, _ in today.values()), sum(f for _, f in today.values())
        total_before = sum(n for n, _ in before.values())
        lines.append(f'Jami: {number(total)} serve, fill {fill(total_filled, total)} '
                     f'(oldingi kunga {change(total, total_before)})')
        ranked = sorted(today.items(), key=lambda item: -item[1][0])
        for client, (count, filled) in ranked[:REPORT_TOP]:
            previous = before.get(client, (0, 0))[0]
            lines.append(f'• {client_label(client)}: {number(count)}, fill {fill(filled, count)}, '
                         f'{change(count, previous)}')
        rest = ranked[REPORT_TOP:]
        if rest:
            lines.append(f'• boshqalar ({len(rest)} ta): {number(sum(count for _, (count, _) in rest))}')
    gone = sorted(client for client, (count, _) in before.items()
                  if client not in today and count >= settings.MONITOR_MIN_HOURLY)
    if gone:
        lines.append('Yo\'qolgan (oldingi kuni bor edi): ' + ', '.join(client_label(client) for client in gone))
    text = '\n'.join(lines)
    if send:
        send_telegram(text)
    return text


# ========================================= Telegram =========================================

def send_telegram(text):
    """Сообщение в чат мониторинга. True — доставлено. Не настроено или не дошло — False (и строка в лог)"""
    token, chat = settings.MONITOR_TELEGRAM_BOT_TOKEN, settings.MONITOR_TELEGRAM_CHAT_ID
    if not token or not chat:
        logger.warning('Мониторинг выдачи: Telegram не настроен (MONITOR_TELEGRAM_BOT_TOKEN / '
                       'MONITOR_TELEGRAM_CHAT_ID), сообщение не отправлено:\n%s', text)
        return False
    if len(text) > TELEGRAM_LIMIT:
        text = text[:TELEGRAM_LIMIT - 1] + '…'
    try:
        response = requests.post(f'https://api.telegram.org/bot{token}/sendMessage', timeout=10,
                                 json={'chat_id': chat, 'text': text, 'disable_web_page_preview': True})
    except requests.RequestException as exc:
        # В тексте исключения requests есть URL, а в нём токен бота — пишем только имя класса
        logger.warning('Мониторинг выдачи: Telegram недоступен (%s)', type(exc).__name__)
        return False
    if response.status_code != 200:
        logger.warning('Мониторинг выдачи: Telegram ответил %s: %s', response.status_code, response.text[:200])
        return False
    return True
