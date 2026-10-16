"""
Медиа объявлений v2: проверки по СОДЕРЖИМОМУ файла (Pillow — фото, ffprobe — видео)
и фоновая обработка видео. Расширение говорит, что ждём, а содержимое обязано ему соответствовать:
PNG с расширением .mp4 ffprobe не примет. Тексты ошибок Telegram — дословно.
"""
import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from decimal import Decimal

from django.core.files import File
from PIL import Image, UnidentifiedImageError

from apps.ads.models import AdMedia
from apps.common.media import SCALE_KEEP_UP_TO_1280, probe_video, transcode_to_h264

logger = logging.getLogger(__name__)

MIN_WIDTH = 640
ASPECT_RATIO = Decimal(16) / Decimal(9)
RATIO_TOLERANCE = Decimal('0.01')  # ±1%
MAX_PHOTO_BYTES = 5 * 1024 * 1024
MAX_VIDEO_BYTES = 20 * 1024 * 1024
MIN_DURATION, MAX_DURATION = 3, 60

ERR_RATIO = 'The aspect ratio must be 16:9.'
ERR_PHOTO_SIZE = 'Photo size must not exceed 5 MB.'
ERR_VIDEO_SIZE = 'Размер видео — не больше 20 МБ'
ERR_WIDTH = f'Ширина — не меньше {MIN_WIDTH} px'
ERR_DURATION = f'Длительность видео — от {MIN_DURATION} до {MAX_DURATION} секунд'
ERR_NOT_PHOTO = 'Файл не похож на фото jpg/png (или не совпадает с расширением)'
ERR_NOT_VIDEO = 'Файл не похож на видео mp4/mov (или не совпадает с расширением)'
ERR_EXTENSION = 'Поддерживаются фото jpg/png и видео mp4/mov'
ERR_PROCESSING = 'Не удалось обработать видео, загрузите другой файл'

PHOTO_FORMATS = {'jpg': 'JPEG', 'jpeg': 'JPEG', 'png': 'PNG'}
VIDEO_EXTENSIONS = ('mp4', 'mov')
EXIF_ORIENTATION = 0x0112  # 5–8 — картинка повёрнута на 90°: показываемые ширина и высота меняются местами


class MediaError(Exception):
    """Файл не подходит — текст для пользователя"""


@dataclass
class MediaInfo:
    media_type: str
    ext: str
    width: int
    height: int
    duration: Decimal | None = None


def inspect_upload(uploaded):
    """Проверка загруженного файла по содержимому. MediaError с понятным текстом, если не подходит"""
    ext = os.path.splitext(uploaded.name or '')[1].lstrip('.').lower()
    if ext in PHOTO_FORMATS:
        return _inspect_photo(uploaded, ext)
    if ext in VIDEO_EXTENSIONS:
        if uploaded.size > MAX_VIDEO_BYTES:
            raise MediaError(ERR_VIDEO_SIZE)
        with _local_path(uploaded, ext) as path:
            return inspect_video(path, ext)
    raise MediaError(ERR_EXTENSION)


def _inspect_photo(uploaded, ext):
    if uploaded.size > MAX_PHOTO_BYTES:
        raise MediaError(ERR_PHOTO_SIZE)
    try:
        uploaded.seek(0)
        with Image.open(uploaded) as image:
            if image.format != PHOTO_FORMATS[ext]:
                raise MediaError(ERR_NOT_PHOTO)
            width, height = image.size
            if image.getexif().get(EXIF_ORIENTATION) in (5, 6, 7, 8):
                width, height = height, width  # проверяем то, что увидит человек
        uploaded.seek(0)
        with Image.open(uploaded) as image:
            image.verify()  # битый / обрезанный файл
    except MediaError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError, ValueError):
        raise MediaError(ERR_NOT_PHOTO)
    finally:
        uploaded.seek(0)
    _check_frame(width, height)
    return MediaInfo(AdMedia.MediaType.PHOTO, ext, width, height)


def inspect_video(path, ext='mp4'):
    """ffprobe (только mp4/mov, только локальный файл): видеодорожка, 16:9, ширина, длительность"""
    try:
        data = probe_video(path)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError, OSError):
        raise MediaError(ERR_NOT_VIDEO)
    stream = next((s for s in data.get('streams', []) if s.get('codec_type') == 'video'), None)
    try:
        width, height = int(stream['width']), int(stream['height'])
        duration = Decimal(str(data['format']['duration'])).quantize(Decimal('0.01'))
    except (TypeError, KeyError, ValueError, ArithmeticError):
        raise MediaError(ERR_NOT_VIDEO)
    if abs(_rotation(stream)) % 180 == 90:
        width, height = height, width  # видео с телефона, повёрнутое метаданными
    _check_frame(width, height)
    if not MIN_DURATION <= duration <= MAX_DURATION:
        raise MediaError(ERR_DURATION)
    return MediaInfo(AdMedia.MediaType.VIDEO, ext, width, height, duration)


def _rotation(stream):
    for item in stream.get('side_data_list') or []:
        if 'rotation' in item:
            return int(float(item['rotation']))
    try:
        return int((stream.get('tags') or {}).get('rotate', 0))
    except ValueError:
        return 0


def _check_frame(width, height):
    if width < MIN_WIDTH:
        raise MediaError(ERR_WIDTH)
    if height <= 0 or abs(Decimal(width) / Decimal(height) - ASPECT_RATIO) > ASPECT_RATIO * RATIO_TOLERANCE:
        raise MediaError(ERR_RATIO)


class _local_path:
    """Путь к загруженному файлу на диске (ffprobe нужен путь): большие Django уже держит во временном файле"""

    def __init__(self, uploaded, ext):
        self.uploaded, self.ext, self.tmp = uploaded, ext, None

    def __enter__(self):
        if hasattr(self.uploaded, 'temporary_file_path'):
            return self.uploaded.temporary_file_path()
        self.tmp = tempfile.NamedTemporaryFile(suffix=f'.{self.ext}', delete=False)
        self.uploaded.seek(0)
        shutil.copyfileobj(self.uploaded, self.tmp)
        self.tmp.close()
        self.uploaded.seek(0)
        return self.tmp.name

    def __exit__(self, *exc):
        if self.tmp:
            os.unlink(self.tmp.name)


def save_upload(user, uploaded):
    """Проверяет файл и сохраняет AdMedia (фото — сразу READY, видео — PROCESSING до перекодирования)"""
    info = inspect_upload(uploaded)
    media = AdMedia(user=user, media_type=info.media_type, width=info.width, height=info.height,
                    duration=info.duration, size=uploaded.size,
                    status=AdMedia.Status.READY if info.media_type == AdMedia.MediaType.PHOTO
                    else AdMedia.Status.PROCESSING)
    uploaded.seek(0)
    media.file.save(f'{media.id}.{info.ext}', uploaded, save=False)  # имя — свой UUID, не имя пользователя
    media.save()
    return media


def process_video(media):
    """
    Фоновая обработка (Celery): перекодирование в H.264 mp4 с +faststart (общая функция),
    проверка РЕЗУЛЬТАТА теми же правилами, замена файла → READY. Любая проблема → FAILED.
    Масштаб: исходный размер, если ширина ≤ 1280, иначе 1280 по ширине с сохранением пропорций.
    """
    input_path = media.file.path
    output_path = os.path.join(os.path.dirname(input_path), f'{uuid.uuid4()}.mp4')
    try:
        transcode_to_h264(input_path, output_path, SCALE_KEEP_UP_TO_1280)
        info = inspect_video(output_path)
    except MediaError as exc:
        return _fail(media, str(exc), output_path)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        logger.error('Не удалось перекодировать видео %s: %s', media.pk, type(exc).__name__)
        return _fail(media, ERR_PROCESSING, output_path)

    old_name = media.file.name
    with open(output_path, 'rb') as result:
        media.file.save(f'{media.id}.mp4', File(result), save=False)
    os.remove(output_path)
    if old_name != media.file.name:
        media.file.storage.delete(old_name)
    media.width, media.height, media.duration = info.width, info.height, info.duration
    media.size, media.status, media.error = media.file.size, AdMedia.Status.READY, ''
    media.save(update_fields=['file', 'width', 'height', 'duration', 'size', 'status', 'error'])


def _fail(media, error, output_path=None):
    if output_path and os.path.exists(output_path):
        os.remove(output_path)
    media.status, media.error = AdMedia.Status.FAILED, error[:255]
    media.save(update_fields=['status', 'error'])
