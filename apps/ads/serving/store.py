"""
Redis выдачи: клиент, ключи и Lua-скрипты (все изменения денег и счётчиков — атомарно в Lua).
Деньги — целые тысячные доли сума. Сутки — по Asia/Tashkent (settings.TIME_ZONE).
"""
import threading
from datetime import timedelta
from zoneinfo import ZoneInfo

import redis
from django.conf import settings
from django.utils import timezone

PREFIX = 'ads:'

VERSION = PREFIX + 'ver'          # версия снимка: INCR при любом изменении объявления
DIRTY = PREFIX + 'dirty'          # объявления с несброшенными дельтами
STOP = PREFIX + 'stop'            # объявления, у которых бюджет кончился, — остановить при сбросе
BATCHES = PREFIX + 'batches'      # ZSET пачек сброса: id → время создания
FLUSH_LOCK = PREFIX + 'flushlock'  # один сброс одновременно

DAILY_TTL = int(timedelta(days=3).total_seconds())   # потрачено за сутки
FREQ_TTL = int(timedelta(days=2).total_seconds())    # показы зрителю за сутки
FROZEN_TTL = 600     # ключ бюджета снят на время возврата / пересчёта
CLICK_WINDOW = 5     # повторный клик того же зрителя по тому же объявлению не считаем, с
RATE_TTL = 70        # счётчик лимита запросов живёт минуту (+ запас)


class ServingUnavailable(Exception):
    """Redis выдачи недоступен — вызывающий отвечает 503 (или пустой выдачей)"""


def budget_key(ad_id):
    """Остаток бюджета, тысячные сума"""
    return f'{PREFIX}b:{ad_id}'


def daily_key(ad_id, day):
    """Потрачено за сутки, тысячные сума"""
    return f'{PREFIX}d:{ad_id}:{day}'


def freq_key(viewer, day):
    """Хеш «объявление → сколько раз показали зрителю за сутки»"""
    return f'{PREFIX}f:{viewer}:{day}'


def event_key(nonce, kind):
    """Отметка «событие по этому токену уже учтено» (kind: v / c)"""
    return f'{PREFIX}e:{nonce}:{kind}'


def click_key(ad_id, viewer):
    return f'{PREFIX}k:{ad_id}:{viewer}'


def delta_key(ad_id):
    """Несброшенные дельты: поля «<день>:v|fv|c|s» (показы, бесплатные показы, клики, потрачено)"""
    return f'{PREFIX}delta:{ad_id}'


def batch_key(batch_id):
    """Какие объявления в пачке сброса"""
    return f'{PREFIX}batch:{batch_id}'


def flush_key(batch_id, ad_id):
    """Дельты объявления, забранные в пачку"""
    return f'{PREFIX}flush:{batch_id}:{ad_id}'


def frozen_key(ad_id):
    """Ключ бюджета снят на время возврата / пересчёта — снимок не должен создавать его заново"""
    return f'{PREFIX}frozen:{ad_id}'


def anomaly_key(ad_id):
    """Суперадмину уже сообщили о трате сверх бюджета по этому объявлению"""
    return f'{PREFIX}anomaly:{ad_id}'


def rate_key(kind, ident, minute):
    return f'{PREFIX}rl:{kind}:{ident}:{minute}'


def tz():
    return ZoneInfo(settings.TIME_ZONE)


def day_of(moment=None):
    """Сутки по Ташкенту: '20261017'"""
    return (moment or timezone.now()).astimezone(tz()).strftime('%Y%m%d')


def to_thousandths(amount):
    """Decimal сумов → целые тысячные (вниз): остаток в Redis никогда не больше настоящего"""
    return int(amount * 1000)


# ============================================ Lua ============================================

# Событие view. Ответ: 1 — списали, 2 — бесплатный показ, 0 — дубль токена, -1 — частота исчерпана
VIEW_LUA = """
if not redis.call('SET', KEYS[1], 1, 'NX', 'EX', ARGV[1]) then return 0 end
local limit = tonumber(ARGV[3])
local seen = tonumber(redis.call('HGET', KEYS[2], ARGV[2]) or '0')
if limit > 0 and seen >= limit then return -1 end
redis.call('HINCRBY', KEYS[2], ARGV[2], 1)
redis.call('EXPIRE', KEYS[2], ARGV[8])
local day = ARGV[7]
local price = tonumber(ARGV[5])
if ARGV[4] == '1' then
  local budget = redis.call('GET', KEYS[3])
  if budget and tonumber(budget) >= price then
    local daily_limit = tonumber(ARGV[6])
    local daily = tonumber(redis.call('GET', KEYS[4]) or '0')
    if daily_limit < 0 or daily + price <= daily_limit then
      local left = redis.call('DECRBY', KEYS[3], price)
      redis.call('INCRBY', KEYS[4], price)
      redis.call('EXPIRE', KEYS[4], ARGV[9])
      redis.call('HINCRBY', KEYS[5], day .. ':v', 1)
      redis.call('HINCRBY', KEYS[5], day .. ':s', price)
      redis.call('SADD', KEYS[6], ARGV[2])
      if left < price then redis.call('SADD', KEYS[7], ARGV[2]) end
      return 1
    end
  end
end
redis.call('HINCRBY', KEYS[5], day .. ':fv', 1)
redis.call('SADD', KEYS[6], ARGV[2])
return 2
"""

# Событие click. Ответ: 1 — засчитан, 0 — дубль токена, -1 — повтор того же зрителя за 5 с
CLICK_LUA = """
if not redis.call('SET', KEYS[1], 1, 'NX', 'EX', ARGV[1]) then return 0 end
if not redis.call('SET', KEYS[2], 1, 'NX', 'EX', ARGV[2]) then return -1 end
redis.call('HINCRBY', KEYS[3], ARGV[3] .. ':c', 1)
redis.call('SADD', KEYS[4], ARGV[4])
return 1
"""

# Сброс: забрать дельты «грязных» объявлений в пачку (RENAME). Ответ: {в пачке, снято из dirty}
GRAB_LUA = """
local ads = redis.call('SPOP', KEYS[1], tonumber(ARGV[4]))
local n = 0
for _, ad in ipairs(ads) do
  local src = ARGV[3] .. 'delta:' .. ad
  if redis.call('EXISTS', src) == 1 then
    redis.call('RENAME', src, ARGV[3] .. 'flush:' .. ARGV[1] .. ':' .. ad)
    redis.call('SADD', KEYS[2], ad)
    n = n + 1
  end
end
if n > 0 then redis.call('ZADD', KEYS[3], ARGV[2], ARGV[1]) end
return {n, #ads}
"""

# Заморозка одного объявления перед возвратом / пересчётом: снять ключ бюджета (новые показы —
# бесплатные), запретить снимку создавать его заново и собрать в свою пачку все несброшенные
# дельты объявления — из delta:{ad} и из пачек сброса, которые ещё не записаны в Postgres.
# Пачки, уже записанные в Postgres, только удаляем. Ответ: 1 — есть что записать, 0 — нечего.
# ARGV: 1 ad, 2 своя пачка, 3 время, 4 метка заморозки, 5 TTL заморозки, 6 префикс,
#       7 число незаписанных пачек N, 8..7+N — незаписанные, дальше — записанные
SETTLE_LUA = """
redis.call('DEL', KEYS[1])
redis.call('SET', KEYS[2], ARGV[4], 'EX', tonumber(ARGV[5]))
redis.call('SREM', KEYS[3], ARGV[1])
local prefix = ARGV[6]
local mine = prefix .. 'flush:' .. ARGV[2] .. ':' .. ARGV[1]
local function merge(src)
  local data = redis.call('HGETALL', src)
  for i = 1, #data, 2 do redis.call('HINCRBY', mine, data[i], data[i + 1]) end
  redis.call('DEL', src)
end
merge(KEYS[4])
local n = tonumber(ARGV[7])
for i = 8, 7 + n do
  merge(prefix .. 'flush:' .. ARGV[i] .. ':' .. ARGV[1])
  redis.call('SREM', prefix .. 'batch:' .. ARGV[i], ARGV[1])
end
for i = 8 + n, #ARGV do
  redis.call('DEL', prefix .. 'flush:' .. ARGV[i] .. ':' .. ARGV[1])
  redis.call('SREM', prefix .. 'batch:' .. ARGV[i], ARGV[1])
end
if redis.call('EXISTS', mine) == 1 then
  redis.call('SADD', prefix .. 'batch:' .. ARGV[2], ARGV[1])
  redis.call('ZADD', KEYS[5], ARGV[3], ARGV[2])
  return 1
end
return 0
"""

# После коммита пересчёта (пополнение): вернуть ключ бюджета, если заморозка всё ещё наша
RESTORE_LUA = """
if redis.call('GET', KEYS[2]) == ARGV[1] then
  redis.call('SET', KEYS[1], ARGV[2])
  redis.call('DEL', KEYS[2])
  return 1
end
return 0
"""

# Снимок: создать ключи бюджета, которых нет (кроме замороженных). ARGV: префикс, пары ad, остаток
SETNX_BUDGETS_LUA = """
local n = 0
for i = 2, #ARGV, 2 do
  if redis.call('EXISTS', ARGV[1] .. 'frozen:' .. ARGV[i]) == 0 then
    if redis.call('SET', ARGV[1] .. 'b:' .. ARGV[i], ARGV[i + 1], 'NX') then n = n + 1 end
  end
end
return n
"""

# Снять замок, только если он наш
RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""


class _Store:
    """Клиент Redis выдачи и зарегистрированные скрипты (EVALSHA с запасным EVAL)"""

    def __init__(self, url):
        self.redis = redis.Redis.from_url(
            url, decode_responses=True, socket_connect_timeout=1, socket_timeout=2, health_check_interval=30)
        self.view = self.redis.register_script(VIEW_LUA)
        self.click = self.redis.register_script(CLICK_LUA)
        self.grab = self.redis.register_script(GRAB_LUA)
        self.settle = self.redis.register_script(SETTLE_LUA)
        self.restore = self.redis.register_script(RESTORE_LUA)
        self.setnx_budgets = self.redis.register_script(SETNX_BUDGETS_LUA)
        self.release = self.redis.register_script(RELEASE_LUA)


_stores = {}
_stores_lock = threading.Lock()


def store():
    """Один клиент на адрес (пул соединений потокобезопасен)"""
    url = settings.ADS_REDIS_URL
    current = _stores.get(url)
    if current is None:
        with _stores_lock:
            current = _stores.get(url) or _Store(url)
            _stores[url] = current
    return current


def over_rate_limit(checks):
    """
    checks — [(kind, ident, limit)]; лимит 0 — без ограничения.
    Один поход в Redis на все счётчики минуты. True — хоть один лимит превышен.
    """
    checks = [check for check in checks if check[2] > 0]
    if not checks:
        return False
    minute = int(timezone.now().timestamp()) // 60
    pipe = store().redis.pipeline(transaction=False)
    for kind, ident, _ in checks:
        key = rate_key(kind, ident, minute)
        pipe.incr(key)
        pipe.expire(key, RATE_TTL)
    results = pipe.execute()
    return any(results[i * 2] > limit for i, (_, _, limit) in enumerate(checks))


def bump_version():
    """Снимок во всех процессах перечитается (фоновый поток проверяет версию раз в секунду)"""
    store().redis.incr(VERSION)
