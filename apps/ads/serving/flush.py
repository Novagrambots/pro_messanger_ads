"""
Сброс показов из Redis в Postgres и «заморозка» объявления перед возвратом денег.

Сброс (beat каждые 5 с): Lua забирает дельты «грязных» объявлений в пачку (RENAME во временные
ключи) → одна транзакция: лок объявлений, Ad.spent += Δ, AdDailyStat += Δ, запись FlushBatch(id)
→ после коммита ключи пачки удаляются. Содержимое пачки читается только ПОСЛЕ лока объявлений,
а FlushBatch(id) — первичный ключ: одна пачка не запишется дважды, даже если процесс упал
между коммитом и удалением ключей (такие пачки досчитывает или чистит следующий сброс).

Заморозка (freeze) — под локом объявления перед refund() и при пополнении: снять ключ бюджета
(новые показы — бесплатные) и записать все несброшенные дельты объявления в той же транзакции.
"""
import logging
import time
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import redis
from django.db import connection, transaction
from django.db.models import F
from django.utils import timezone

from apps.ads.models import Ad, AdDailyStat, AdStatus, FlushBatch

from . import store as S

logger = logging.getLogger(__name__)

GRAB_SIZE = 500       # объявлений в одной пачке
RECOVER_AFTER = 60    # пачка старше минуты без FlushBatch — сброс упал, досчитываем
LOCK_TTL = 30         # замок сброса, с
TIME_BUDGET = 20      # один запуск сброса — не дольше, с (остальное заберёт следующий)
KEEP_BATCHES = timedelta(days=7)
PRUNE_KEY = S.PREFIX + 'prune'


def flush():
    """Сбросить всё накопленное. Возвращает, по скольким объявлениям записали дельты"""
    store = S.store()
    lock = uuid.uuid4().hex
    if not store.redis.set(S.FLUSH_LOCK, lock, nx=True, ex=LOCK_TTL):
        return 0  # сброс уже идёт
    started = time.monotonic()
    try:
        done = 0
        for batch_id in store.redis.zrangebyscore(S.BATCHES, '-inf', time.time() - RECOVER_AFTER):
            done += apply_batch(batch_id)
        while time.monotonic() - started < TIME_BUDGET:
            batch_id = uuid.uuid4().hex
            grabbed, popped = store.grab(keys=[S.DIRTY, S.batch_key(batch_id), S.BATCHES],
                                         args=[batch_id, time.time(), S.PREFIX, GRAB_SIZE])
            if grabbed:
                done += apply_batch(batch_id)
            if popped < GRAB_SIZE:
                break
        stop_exhausted()
        _prune_batches()
        return done
    finally:
        store.release(keys=[S.FLUSH_LOCK], args=[lock])


def apply_batch(batch_id):
    """Записать пачку в Postgres (своя транзакция)"""
    ad_ids = sorted(S.store().redis.smembers(S.batch_key(batch_id)))
    if not ad_ids:
        _cleanup(batch_id, ad_ids)
        return 0
    with transaction.atomic():
        return _apply_locked(batch_id, ad_ids)


def _apply_locked(batch_id, ad_ids):
    """Внутри транзакции: лок объявлений → проверка FlushBatch → чтение дельт → запись"""
    ads = {str(ad.pk): ad for ad in Ad.objects.select_for_update().filter(pk__in=ad_ids).order_by('pk')}
    transaction.on_commit(lambda: _cleanup(batch_id, ad_ids))
    if FlushBatch.objects.filter(pk=batch_id).exists():
        return 0  # уже записана (упали до удаления ключей) — только убрать ключи

    pipe = S.store().redis.pipeline(transaction=False)
    for ad_id in ad_ids:
        pipe.hgetall(S.flush_key(batch_id, ad_id))
    rows = []
    for ad_id, fields in zip(ad_ids, pipe.execute()):
        ad = ads.get(ad_id)
        if ad is None or not fields:
            continue
        per_day = _parse_delta(fields)
        spent = _clamp_to_budget(ad, per_day)
        if spent:
            Ad.objects.filter(pk=ad.pk).update(spent=F('spent') + spent)
        rows += [(ad.pk, day, d['v'], d['fv'], d['c'], Decimal(d['s']) / 1000) for day, d in per_day.items()]
    _upsert_stats(rows)
    FlushBatch.objects.create(id=batch_id, ads_count=len(ad_ids))
    return len(ads)


def _parse_delta(fields):
    """{'20261017:v': '3', ...} → {date: {'v': 3, 'fv': 0, 'c': 0, 's': 3000}}"""
    per_day = {}
    for field, value in fields.items():
        day, kind = field.split(':')
        counters = per_day.setdefault(datetime.strptime(day, '%Y%m%d').date(), {'v': 0, 'fv': 0, 'c': 0, 's': 0})
        counters[kind] += int(value)
    return per_day


def _clamp_to_budget(ad, per_day):
    """
    Трата пачки в сумах. Больше остатка (budget − refunded − spent) быть не должно — Redis не
    списывает сверх ключа бюджета. Если всё же так: обрезаем (иначе ограничение в БД навсегда
    заблокирует сброс), пишем ошибку в лог и сообщаем суперадмину — один раз на объявление.
    """
    total = sum(d['s'] for d in per_day.values())
    available = max(S.to_thousandths(ad.budget - ad.refunded - ad.spent), 0)
    if total > available:
        excess = total - available
        logger.error('Сброс показов: трата %s больше остатка %s по объявлению %s — обрезано',
                     Decimal(total) / 1000, Decimal(available) / 1000, ad.pk)
        _alert_overspend(ad, Decimal(excess) / 1000)
        for day in sorted(per_day, reverse=True):  # срезаем с последних суток
            cut = min(per_day[day]['s'], excess)
            per_day[day]['s'] -= cut
            excess -= cut
        total = available
    return Decimal(total) / 1000


def _alert_overspend(ad, excess):
    try:
        first = S.store().redis.set(S.anomaly_key(ad.pk), 1, nx=True)
    except redis.RedisError:
        first = False
    if first:
        from apps.notifications.services import notify_ad_money_alert
        notify_ad_money_alert(ad, f'показы на {excess} сум сверх остатка бюджета не записаны (обрезано при сбросе)')


def _upsert_stats(rows):
    """AdDailyStat += дельты одним INSERT ... ON CONFLICT DO UPDATE"""
    if not rows:
        return
    table = connection.ops.quote_name(AdDailyStat._meta.db_table)
    values = ', '.join(['(%s, %s, %s, %s, %s, %s)'] * len(rows))
    sql = (f'INSERT INTO {table} (ad_id, date, views, free_views, clicks, spent) VALUES {values} '
           f'ON CONFLICT (ad_id, date) DO UPDATE SET '
           f'views = {table}.views + EXCLUDED.views, free_views = {table}.free_views + EXCLUDED.free_views, '
           f'clicks = {table}.clicks + EXCLUDED.clicks, spent = {table}.spent + EXCLUDED.spent')
    with connection.cursor() as cursor:
        cursor.execute(sql, [value for row in rows for value in row])


def _cleanup(batch_id, ad_ids):
    """После коммита: удалить ключи пачки. Не вышло — досчитает / почистит следующий сброс"""
    try:
        pipe = S.store().redis.pipeline(transaction=False)
        for ad_id in ad_ids:
            pipe.delete(S.flush_key(batch_id, ad_id))
        pipe.delete(S.batch_key(batch_id))
        pipe.zrem(S.BATCHES, batch_id)
        pipe.execute()
    except redis.RedisError:
        logger.warning('Не удалось удалить ключи пачки сброса %s — уберёт следующий сброс', batch_id)


def stop_exhausted():
    """Объявления, у которых в Redis остаток меньше цены показа: ACTIVE → STOPPED"""
    r = S.store().redis
    for ad_id in r.smembers(S.STOP):
        with transaction.atomic():
            ad = Ad.objects.select_for_update().filter(pk=ad_id).first()
            left = r.get(S.budget_key(ad_id))
            # Ключа нет — объявление заморожено (пополнение) или Redis потерял данные: не трогаем
            if ad and ad.status == AdStatus.ACTIVE and left is not None and int(left) < ad.cpm:
                ad.stop()
        r.srem(S.STOP, ad_id)


def _prune_batches():
    """Раз в час: записи FlushBatch старше недели (их ключи давно удалены)"""
    if S.store().redis.set(PRUNE_KEY, 1, nx=True, ex=3600):
        FlushBatch.objects.filter(created_at__lt=timezone.now() - KEEP_BATCHES).delete()


# ======================================= Заморозка =======================================

def freeze(ad):
    """
    Вызывать внутри transaction.atomic на объявлении под select_for_update.
    Снимает ключ бюджета в Redis и записывает несброшенные показы объявления в этой же
    транзакции (ad.spent обновляется). Возвращает метку заморозки — для restore_after_commit.
    Redis недоступен → ServingUnavailable (вызывающий отвечает 503, деньги не двигаются).
    """
    store = S.store()
    ad_id = str(ad.pk)
    batch_id = uuid.uuid4().hex
    mark = uuid.uuid4().hex
    try:
        # Пачки сброса, в которых есть дельты этого объявления
        batches = store.redis.zrange(S.BATCHES, 0, -1)
        pipe = store.redis.pipeline(transaction=False)
        for other in batches:
            pipe.exists(S.flush_key(other, ad_id))
        found = [other for other, exists in zip(batches, pipe.execute()) if exists]
        # Уже записанные в Postgres — только удалить; остальные забрать в свою пачку.
        # Сброс не запишет пачку с этим объявлением, пока мы держим его лок (он лочит до чтения)
        written = {pk.hex for pk in FlushBatch.objects.filter(pk__in=found).values_list('pk', flat=True)}
        unwritten = [other for other in found if other not in written]
        has_deltas = store.settle(
            keys=[S.budget_key(ad_id), S.frozen_key(ad_id), S.DIRTY, S.delta_key(ad_id), S.BATCHES],
            args=[ad_id, batch_id, time.time(), mark, S.FROZEN_TTL, S.PREFIX, len(unwritten),
                  *unwritten, *written],
        )
    except redis.RedisError as exc:
        raise S.ServingUnavailable('Redis выдачи недоступен') from exc

    if has_deltas:
        _apply_locked(batch_id, [ad_id])
        ad.refresh_from_db(fields=['spent'])
    return mark


def restore_after_commit(ad, mark):
    """После коммита пополнения: вернуть ключ бюджета с новым остатком (если заморозка всё ещё наша)"""
    ad_id, value = str(ad.pk), S.to_thousandths(ad.remaining_budget())

    def restore():
        try:
            S.store().restore(keys=[S.budget_key(ad_id), S.frozen_key(ad_id)], args=[mark, value])
        except redis.RedisError:
            # Ключ вернёт снимок, когда истечёт заморозка (до 10 минут показы — бесплатные)
            logger.warning('Не удалось вернуть ключ бюджета объявления %s после пополнения', ad_id)

    transaction.on_commit(restore)
