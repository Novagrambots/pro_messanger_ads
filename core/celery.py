import os
from celery import Celery
from celery.schedules import crontab

# Указываем Django, где искать настройки
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')

app = Celery('core', broker_connection_retry=False, broker_connection_retry_on_startup=True)

app.config_from_object('django.conf:settings')

# Автоматически находим tasks.py во всех приложениях
app.autodiscover_tasks()

# Очереди: сброс показов объявлений — своя (свой воркер celery_flush, concurrency 1),
# перекодирование видео — своя (media), всё остальное — очередь по умолчанию 'celery'.
# Так долгое видео не задерживает сброс. Локально воркер: -Q celery,media,serving_flush
app.conf.task_routes = {
    'apps.ads.tasks.flush_ad_stats': {'queue': 'serving_flush'},
    'apps.ads.tasks.process_ad_media': {'queue': 'media'},
}
app.conf.beat_schedule = {
    'remove-unused-media-files_every_day_at_12_00_AM': {
        'task': 'apps.chat_ads.tasks.cleanup_orphaned_media',
        'schedule': crontab(minute=0, hour=0),
        # 'schedule': crontab(),
    },
    # Деньги AdGram: добивание PENDING, компенсация списаний без заказа, алерты о зависших
    'process-money-operations-every-minute': {
        'task': 'apps.billing.tasks.process_money_operations',
        'schedule': crontab(),
    },
    # Объявления v2: наступил start_date → ACTIVE, наступил end_date → ON_HOLD
    'apply-ad-dates-every-minute': {
        'task': 'apps.ads.tasks.apply_ad_dates',
        'schedule': crontab(),
    },
    # Объявления v2: показы, клики и траты из Redis выдачи → Postgres; кончился бюджет → STOPPED.
    # expires — если воркер занят (перекодирует видео), устаревшие запуски выбрасываются, а не копятся
    'flush-ad-stats-every-5-seconds': {
        'task': 'apps.ads.tasks.flush_ad_stats',
        'schedule': 5.0,
        'options': {'expires': 5},
    },
    # Выдача v2: свежая гео-база DB-IP Lite City (страна и город зрителя по IP) — 3-го числа каждого месяца
    'update-geoip-monthly': {
        'task': 'apps.ads.tasks.update_geoip',
        'schedule': crontab(minute=0, hour=4, day_of_month='3'),
    },
    # Объявления v2: удалить медиа старше суток, которые никуда не прикрепили
    'cleanup-ad-media-daily': {
        'task': 'apps.ads.tasks.cleanup_ad_media',
        'schedule': crontab(minute=30, hour=0),
    },
    # Возврат дробного остатка бюджета завершённых заказов
    'refund-completed-orders-every-5-minutes': {
        'task': 'apps.billing.tasks.refund_completed_orders',
        'schedule': crontab(minute='*/5'),
    },
}
