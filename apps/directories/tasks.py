from core.celery import app

from apps.directories.models import TelegramPeer
from apps.directories.telegram import Deadline, resolve_many

REFRESH_SECONDS = 30


@app.task
def refresh_peers(usernames):
    """
    Новые записи из «похожих» сразу проверяем резолвером (тип, числовой id, аватар),
    чтобы к сохранению объявления они были свежими. Не ответил Telegram — не страшно:
    проверим при сохранении объявления
    """
    names = [peer.username for peer in TelegramPeer.objects.filter(username__in=usernames) if not peer.is_fresh()]
    if names:
        resolve_many(names, Deadline(REFRESH_SECONDS))
