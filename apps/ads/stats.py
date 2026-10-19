"""
Метрики объявлений, как у Telegram: CTR = клики / показы, CVR = действия / клики,
CPC = потрачено / клики, CPA = потрачено / действия. Деление на ноль → None (фронт покажет «–»).
Показы — все (платные + бесплатные): клики бывают и на бесплатных показах.
"""
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from apps.billing.services import format_money

from .models import AdDailyStat, Placement, USERS_PLACEMENTS

RATIO_PLACES = 6
MONEY_STEP = Decimal('0.000001')


def ratio(part, whole):
    """Доля с 6 знаками или None"""
    return round(part / whole, RATIO_PLACES) if whole else None


def money_per(spent, count):
    """Сумма на одно событие (6 знаков, через format_money) или None"""
    if not count:
        return None
    return format_money((Decimal(spent) / count).quantize(MONEY_STEP, ROUND_HALF_UP))


def metrics(views, clicks, actions, spent):
    return {'ctr': ratio(clicks, views), 'cvr': ratio(actions, clicks),
            'cpc': money_per(spent, clicks), 'cpa': money_per(spent, actions)}


def targeting_summary(ad):
    """
    Сводка таргетинга без текста: {kind, count} — формулировку («36 bots», «3 queries») делает фронт.
    Боты не выбраны — показ в любых ботах: kind all_bots, count null
    """
    targeting = ad.targeting or {}

    def count(kind):
        return {'kind': kind, 'count': len(targeting.get(kind) or [])}

    if ad.placement == Placement.SEARCH:
        return count('queries')
    if ad.placement == Placement.BOT:
        return count('bots') if targeting.get('bots') else {'kind': 'all_bots', 'count': None}
    if ad.placement == Placement.CHANNEL_MESSAGE:
        return count('channels') if targeting.get('channels') else count('languages')
    if ad.placement in USERS_PLACEMENTS:
        return count('locations') if targeting.get('locations') else count('countries')
    return {'kind': 'none', 'count': None}


def period(ad, start, end):
    """Статистика за дни start…end (по Ташкенту, включительно): каждый день, итоги и метрики"""
    rows = {row.date: row for row in AdDailyStat.objects.filter(ad=ad, date__range=(start, end))}
    days, totals = [], {'views': 0, 'free_views': 0, 'clicks': 0, 'actions': 0, 'video_opens': 0,
                        'spent': Decimal('0')}
    day = start
    while day <= end:
        row = rows.get(day)
        values = {field: getattr(row, field) if row else 0 for field in totals}
        for field, value in values.items():
            totals[field] += value
        days.append({'date': day, **values, 'spent': format_money(Decimal(values['spent']))})
        day += timedelta(days=1)
    summary = metrics(totals['views'] + totals['free_views'], totals['clicks'], totals['actions'], totals['spent'])
    return {'from': start, 'to': end, 'days': days,
            'totals': {**totals, 'spent': format_money(totals['spent']), **summary}}
