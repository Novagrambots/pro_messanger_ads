"""
Выдача объявлений v2 (горячий путь) — без DRF.

Postgres — правда о деньгах, Redis выдачи — рабочая копия:
- snapshot   — снимок ACTIVE-объявлений в памяти процесса (обновляет фоновый поток);
- selection  — выбор объявления на запрос рекламы (аукцион по CPM);
- events     — события view / click: деньги списываются в Redis Lua-скриптом;
- flush      — сброс дельт в Postgres пачками (beat каждые 5 с) и «заморозка» перед возвратом денег;
- tokens     — viewer_key и подписанный random_id;
- store      — клиент Redis, ключи, Lua-скрипты;
- views      — простые Django-вьюхи /api/ads/serve/, /api/ads/events/ и /api/ads/report/.

Деньги в Redis — только целые тысячные доли сума: цена показа в тысячных = cpm.
На горячем пути (serve, events) запросов к Postgres нет.
"""
