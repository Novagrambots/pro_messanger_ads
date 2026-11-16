# NovaGram Ads — бэкенд

Django REST бэкенд рекламной платформы NovaGram. API-документация — `/api/docs/`.

## Запуск

Локально (venv в `.venv`, нужны PostgreSQL и Redis; настройки — в `.env`, пример — `.env.example`):

```bash
python manage.py migrate
python manage.py runserver

# Celery: один воркер на все три очереди — иначе видео и сброс показов не обработаются
celery -A core worker -l info -Q celery,media,serving_flush
celery -A core beat -l info
```

Очереди Celery (маршруты — `core/celery.py`): `celery` — по умолчанию (деньги, уведомления, чистка),
`media` — перекодирование видео объявлений, `serving_flush` — сброс показов выдачи из Redis в Postgres каждые 5 с.

Гео-база для формата «Пользователи» (страна и город зрителя по IP; IP Geolocation by DB-IP, CC BY 4.0) —
скачать один раз, дальше её раз в месяц обновляет beat (файл — `GEOIP_PATH`, в git не хранится):

```bash
python manage.py update_geoip
```

Старые версии приложения ходят в старые URL выдачи (`/api/search_ads/order/search/`, `/api/chat_ads/order/search/`,
`…/order/click/`). Мост `apps/ads/serving/legacy.py` показывает там и объявления v2 — с общими бюджетом, частотой
показов и статистикой (тег = фраза поиска, `channel_id` = канал; объявления v2 — только числовому `viewer_id`).
Выключатель — `ADS_LEGACY_BRIDGE=False`: старые URL снова отдают только старые заказы.

За Cloudflare задайте в `.env` `CLIENT_IP_HEADER=CF-Connecting-IP` — иначе IP берётся из первого адреса
`X-Forwarded-For`, а его клиент может подделать.

Тесты (нужен Redis для выдачи; эта база очищается перед каждым тестом):

```bash
ADS_REDIS_TEST_URL=redis://localhost:6379/15 python manage.py test
```

Docker:

```bash
docker compose up -d --build
docker compose restart nginx   # обязательно после пересоздания web / serving, иначе 502
```

| Сервис | Что делает |
|---|---|
| `db`, `redis` | PostgreSQL; Redis — брокер Celery и кэш Django |
| `redis_ads` | Redis выдачи объявлений: бюджеты и счётчики показов (AOF, noeviction, `ADS_REDIS_MAXMEMORY`) |
| `web` | gunicorn `:9000` — кабинет, модерация, админка; при старте `migrate` и `collectstatic` |
| `serving` | gunicorn `:9100` — только `/api/ads/serve/`, `events/`, `report/` (`SERVING_ONLY=True`; ядра, воркеры, потоки — `SERVING_CPUS/WORKERS/THREADS`) |
| `celery_worker` | `-Q celery,media --concurrency=2`; раз в месяц качает гео-базу в volume `geo_data` |
| `celery_flush` | `-Q serving_flush --concurrency=1` — сброс показов, отдельно от видео |
| `celery_beat` | расписание задач |
| `nginx` | `127.0.0.1:18080`; горячий путь выдачи → `serving`, остальное → `web` |
