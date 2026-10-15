from core.celery import app

from apps.ads.models import Ad


@app.task
def apply_ad_dates():
    """Раз в минуту: наступил start_date → ACTIVE, наступил end_date → ON_HOLD"""
    Ad.apply_due_dates()
