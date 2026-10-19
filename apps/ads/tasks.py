import logging
import uuid
from datetime import timedelta

import redis
from django.db.models import ProtectedError
from django.utils import timezone

from core.celery import app

from apps.ads.media import process_video
from apps.ads.models import Ad, AdDraft, AdMedia

logger = logging.getLogger(__name__)


@app.task
def apply_ad_dates():
    """Раз в минуту: наступил start_date → ACTIVE, наступил end_date → ON_HOLD"""
    Ad.apply_due_dates()


@app.task
def process_ad_media(media_id):
    """Перекодирование загруженного видео в H.264 mp4 и проверка результата → READY / FAILED"""
    media = AdMedia.objects.filter(pk=media_id, status=AdMedia.Status.PROCESSING).first()
    if media:
        process_video(media)


def draft_media_ids():
    """id медиа из черновиков (в черновике может лежать что угодно — берём только корректные UUID)"""
    ids = []
    for value in AdDraft.objects.exclude(data__media=None).values_list('data__media', flat=True):
        try:
            ids.append(uuid.UUID(str(value)))
        except ValueError:
            continue
    return ids


@app.task
def cleanup_ad_media():
    """Раз в сутки: медиа старше 24 ч, на которые не ссылается ни одно объявление, правка или черновик"""
    orphans = AdMedia.objects.filter(created_at__lt=timezone.now() - timedelta(hours=24),
                                     ads__isnull=True, revisions__isnull=True).exclude(pk__in=draft_media_ids())
    deleted = 0
    for media in orphans:
        file_name = media.file.name
        try:
            media.delete()  # PROTECT: если объявление успело сослаться — строка (и файл) останутся
        except ProtectedError:
            continue
        if file_name:
            media.file.storage.delete(file_name)
        deleted += 1
    logger.info('Удалено неиспользуемых медиа объявлений: %s', deleted)
    return deleted



@app.task(ignore_result=True)
def flush_ad_stats():
    """Каждые 5 с: показы, клики и траты из Redis выдачи → Postgres; кончился бюджет → STOPPED"""
    from apps.ads.serving.flush import flush
    try:
        flush()
    except redis.RedisError:
        # Redis выдачи лежит — сбрасывать нечего, дельты дождутся (одна строка вместо трейсбека каждые 5 с)
        logger.warning('Сброс показов пропущен: Redis выдачи недоступен')


@app.task(ignore_result=True)
def update_geoip():
    """Раз в месяц: свежая гео-база DB-IP Lite City (файл подменяется атомарно, процессы подхватят сами)"""
    from apps.ads.serving.geo import update_database
    update_database()
