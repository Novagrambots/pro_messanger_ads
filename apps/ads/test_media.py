"""
Тесты медиа объявлений v2 (этап 6). Файлы настоящие, генерируются в тесте: фото — Pillow,
видео — ffmpeg (если его нет в окружении, видео-тесты пропускаются с понятной причиной).
"""
import http.server
import io
import os
import shutil
import socketserver
import subprocess
import tempfile
import threading
from datetime import timedelta
from decimal import Decimal
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import TestCase
from django.utils import timezone
from PIL import Image

from apps.ads import media as ad_media
from apps.ads.models import Ad, AdMedia, AdPricing, AdRevision, Placement
from apps.ads.tasks import cleanup_ad_media, process_ad_media
from apps.ads.tests import KUNUZ_ID, client_for, create_ad, make_local_user, setUpModule, tearDownModule  # noqa: F401
from apps.chat_ads.models import ChatAdMedia
from apps.common import media as common_media
from apps.directories.models import TelegramPeer
from apps.directories.telegram import store_avatar
from apps.directories.tests import TempMediaMixin

User = get_user_model()
HAS_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))
needs_ffmpeg = skipUnless(HAS_FFMPEG, 'В окружении нет ffmpeg/ffprobe — тесты с настоящим видео пропущены')
Status, Type = AdMedia.Status, AdMedia.MediaType


def image_bytes(width, height, fmt='JPEG', noise=False, orientation=None):
    if noise:  # шум не сжимается — так легко получить файл больше 5 МБ
        image = Image.frombytes('RGB', (width, height), os.urandom(width * height * 3))
    else:
        image = Image.new('RGB', (width, height), (30, 120, 200))
    extra = {}
    if orientation:
        exif = Image.Exif()
        exif[ad_media.EXIF_ORIENTATION] = orientation
        extra['exif'] = exif
    buffer = io.BytesIO()
    image.save(buffer, fmt, **extra)
    return buffer.getvalue()


_videos = {}


def video_bytes(seconds, size='640x360', container='mp4'):
    """Настоящее видео из тестового генератора ffmpeg (кэшируется на время прогона)"""
    key = (seconds, size, container)
    if key not in _videos:
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, f'video.{container}')
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', f'testsrc=size={size}:rate=5',
                            '-t', str(seconds), '-pix_fmt', 'yuv420p', '-c:v', 'libx264', '-preset', 'ultrafast',
                            '-y', path], check=True, timeout=120)
            with open(path, 'rb') as file:
                _videos[key] = file.read()
    return _videos[key]


def run_now(media_id):
    """Вместо Celery — обрабатываем сразу"""
    process_ad_media(media_id)


class MediaBase(TempMediaMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = make_local_user()
        self.client = client_for(self.user)

    def upload(self, name, content, client=None):
        with patch('apps.ads.tasks.process_ad_media.delay', side_effect=run_now), \
                self.captureOnCommitCallbacks(execute=True):
            return (client or self.client).post('/api/ads/media/', {'file': SimpleUploadedFile(name, content)},
                                                format='multipart')

    def photo(self, user=None, **kwargs):
        response = self.upload('photo.jpg', image_bytes(1280, 720, **kwargs), client_for(user) if user else None)
        self.assertEqual(response.status_code, 201, response.data)
        return AdMedia.objects.get(pk=response.data['id'])

    def ready_video(self, user=None):
        """READY-видео без перекодирования (для правил объявления и наценок)"""
        return AdMedia.objects.create(user=user or self.user, media_type=Type.VIDEO, status=Status.READY,
                                      width=1280, height=720, duration=Decimal('10'),
                                      file=ContentFile(b'video', name='video.mp4'))

    def assert_rejected(self, response, message):
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(str(response.data['file']), message)


class PhotoUploadTests(MediaBase):
    def test_photo_ok(self):
        response = self.upload('promo.jpg', image_bytes(1280, 720))
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual((response.data['type'], response.data['status'], response.data['width'],
                          response.data['height']), ('PHOTO', 'READY', 1280, 720))
        self.assertTrue(response.data['url'].startswith('http://testserver/media/ads/media/'))
        self.assertNotIn('promo', response.data['url'])  # имя файла — свой UUID

        self.assertEqual(self.upload('promo.png', image_bytes(1920, 1080, 'PNG')).status_code, 201)
        self.assertEqual(self.upload('almost.jpg', image_bytes(1280, 725)).status_code, 201)  # 16:9 ±1%

    def test_photo_rules(self):
        self.assert_rejected(self.upload('4x3.jpg', image_bytes(1024, 768)), ad_media.ERR_RATIO)
        self.assert_rejected(self.upload('off.jpg', image_bytes(1280, 730)), ad_media.ERR_RATIO)  # >1%
        self.assert_rejected(self.upload('small.jpg', image_bytes(600, 338)), ad_media.ERR_WIDTH)

        big = image_bytes(1920, 1080, 'PNG', noise=True)
        self.assertGreater(len(big), 5 * 1024 * 1024)
        self.assert_rejected(self.upload('big.png', big), ad_media.ERR_PHOTO_SIZE)

        self.assert_rejected(self.upload('jpeg.png', image_bytes(1280, 720)), ad_media.ERR_NOT_PHOTO)
        self.assert_rejected(self.upload('photo.mp4', image_bytes(1280, 720, 'PNG')), ad_media.ERR_NOT_VIDEO)
        self.assert_rejected(self.upload('anim.gif', b'GIF89a'), ad_media.ERR_EXTENSION)
        self.assert_rejected(self.upload('broken.jpg', image_bytes(1280, 720)[:200]), ad_media.ERR_NOT_PHOTO)
        self.assertEqual(self.client.post('/api/ads/media/', {}, format='multipart').status_code, 400)
        self.assertFalse(AdMedia.objects.exists())

    def test_exif_rotation(self):
        """Хранится 720×1280, но повёрнуто EXIF'ом — человек видит 1280×720"""
        response = self.upload('rotated.jpg', image_bytes(720, 1280, orientation=6))
        self.assertEqual((response.status_code, response.data['width'], response.data['height']), (201, 1280, 720))

    def test_too_big_video_rejected_before_ffprobe(self):
        self.assert_rejected(self.upload('big.mp4', b'\0' * (20 * 1024 * 1024 + 1)), ad_media.ERR_VIDEO_SIZE)

    def test_status_only_own(self):
        media = self.photo()
        self.assertEqual(self.client.get(f'/api/ads/media/{media.pk}/').data['status'], 'READY')
        stranger = client_for(make_local_user('stranger'))
        self.assertEqual(stranger.get(f'/api/ads/media/{media.pk}/').status_code, 404)


@needs_ffmpeg
class VideoTests(MediaBase):
    def probe(self, media):
        return common_media.probe_video(media.file.path)

    def test_video_is_transcoded_without_stretching(self):
        response = self.upload('clip.mp4', video_bytes(4))
        self.assertEqual((response.status_code, response.data['status']), (201, 'PROCESSING'))  # ответ до обработки

        media = AdMedia.objects.get(pk=response.data['id'])
        self.assertEqual((media.status, media.width, media.height), (Status.READY, 640, 360))  # 640 не растянули
        self.assertTrue(media.file.name.endswith('.mp4'))
        self.assertEqual(Decimal('4'), media.duration.quantize(Decimal('1')))
        stream = self.probe(media)['streams'][0]
        self.assertEqual(stream['codec_name'], 'h264')

    def test_big_video_scaled_down_to_1280(self):
        media = AdMedia.objects.get(pk=self.upload('hd.mp4', video_bytes(3.5, '1920x1080')).data['id'])
        self.assertEqual((media.status, media.width, media.height), (Status.READY, 1280, 720))

    def test_mov_accepted_and_becomes_mp4(self):
        response = self.upload('iphone.mov', video_bytes(4, container='mov'))
        self.assertEqual(response.status_code, 201, response.data)
        media = AdMedia.objects.get(pk=response.data['id'])
        self.assertEqual(media.status, Status.READY)
        self.assertTrue(media.file.name.endswith('.mp4'))
        self.assertFalse(any(name.endswith('.mov') for name in os.listdir(os.path.dirname(media.file.path))))

    def test_video_rules(self):
        self.assert_rejected(self.upload('short.mp4', video_bytes(2)), ad_media.ERR_DURATION)
        self.assert_rejected(self.upload('long.mp4', video_bytes(61, '640x360')), ad_media.ERR_DURATION)
        self.assert_rejected(self.upload('4x3.mp4', video_bytes(4, '640x480')), ad_media.ERR_RATIO)
        self.assert_rejected(self.upload('small.mp4', video_bytes(4, '480x270')), ad_media.ERR_WIDTH)
        self.assertFalse(AdMedia.objects.exists())

    def test_processing_failure_and_broker_down(self):
        error = subprocess.CalledProcessError(1, 'ffmpeg')
        with patch('apps.ads.media.transcode_to_h264', side_effect=error), \
                self.assertLogs('apps.ads.media', level='ERROR'):
            media = AdMedia.objects.get(pk=self.upload('clip.mp4', video_bytes(4)).data['id'])
        self.assertEqual((media.status, media.error), (Status.FAILED, ad_media.ERR_PROCESSING))

        with patch('apps.ads.tasks.process_ad_media.delay', side_effect=ConnectionError('redis')), \
                self.assertLogs('apps.ads.views', level='ERROR'), self.captureOnCommitCallbacks(execute=True):
            response = self.client.post('/api/ads/media/', {'file': SimpleUploadedFile('c.mp4', video_bytes(4))},
                                        format='multipart')
        media = AdMedia.objects.get(pk=response.data['id'])
        self.assertEqual(media.status, Status.FAILED)
        self.assertIn('загрузите ещё раз', media.error)


@needs_ffmpeg
class FfmpegSecurityTests(MediaBase):
    """«mp4», внутри которого HLS-плейлист, не должен заставить ffmpeg ходить по сети — оба пути"""

    def setUp(self):
        super().setUp()
        self.hits = []
        hits = self.hits

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(404)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = socketserver.TCPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        port = self.server.server_address[1]
        self.playlist = (f'#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\n'
                         f'http://127.0.0.1:{port}/segment.ts\n#EXT-X-ENDLIST\n').encode()

    def test_flags_go_before_input(self):
        """Флаги защиты — подряд и РАНЬШЕ входного файла (иначе ffmpeg применит их не ко входу)"""
        for command, target in ((common_media.h264_command('in.mp4', 'out.mp4', '1280:720'), '-i'),
                                (common_media.probe_command('in.mp4'), 'in.mp4')):
            start = command.index('-f')
            self.assertEqual(command[start:start + 4], ['-f', 'mov', '-protocol_whitelist', 'file'])
            self.assertLess(start, command.index(target))

    def test_ads_v2_upload(self):
        self.assert_rejected(self.upload('playlist.mp4', self.playlist), ad_media.ERR_NOT_VIDEO)
        self.assertEqual(self.hits, [])

    def test_old_chat_ads_conversion(self):
        old = ChatAdMedia(user=self.user, media_type=ChatAdMedia.MediaType.VIDEO)
        old.file.save('playlist.mp4', ContentFile(self.playlist), save=False)
        with patch('apps.chat_ads.models.transaction.on_commit'):  # не ставим старую задачу в очередь
            old.save()
        with self.assertRaises(subprocess.CalledProcessError), self.assertLogs('apps.chat_ads.models', 'ERROR'):
            old.convert_to_h264()
        self.assertEqual(self.hits, [])

        # Старый путь по-прежнему 1280×720 — через общую функцию
        with patch('apps.chat_ads.models.transcode_to_h264') as transcode, self.assertRaises(OSError), \
                self.assertLogs('apps.chat_ads.models', 'ERROR'):
            old.convert_to_h264()  # результата нет (функция подменена) — открыть файл не выйдет
        self.assertEqual(transcode.call_args.args[2], common_media.SCALE_CHAT_ADS)


class AdMediaRulesTests(MediaBase):
    """Медиа в объявлении: где можно, с чем нельзя, чьё и в каком статусе"""

    def create(self, placement=Placement.CHANNEL_MESSAGE, targeting=None, **overrides):
        targeting = targeting or ({'languages': ['uz']} if placement == Placement.CHANNEL_MESSAGE
                                  else {'countries': ['UZ']})
        return create_ad(self.client, placement, targeting=targeting, **overrides)

    def test_attach_photo(self):
        photo = self.photo()
        response = self.create(media=str(photo.pk), cpm=1500)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['media']['id'], str(photo.pk))
        self.assertEqual(set(response.data['media']), {'id', 'type', 'url', 'width', 'height', 'duration'})
        self.assertEqual(Ad.objects.get(pk=response.data['id']).media, photo)

        response = self.create(Placement.USERS_CHANNEL_MESSAGE, media=str(photo.pk), cpm=1500)
        self.assertEqual(response.status_code, 201, response.data)

    def test_attach_rules(self):
        photo = self.photo()
        for placement in (Placement.SEARCH, Placement.BOT, Placement.USERS_VIDEO_BANNER, Placement.USERS_BOT_BANNER):
            with self.subTest(placement=placement):
                targeting = {'queries': ['новости uz']} if placement == Placement.SEARCH else (
                    {'bots': ['weatherbot']} if placement == Placement.BOT else {'countries': ['UZ']})
                response = create_ad(self.client, placement, targeting=targeting, media=str(photo.pk), cpm=5000)
                self.assertEqual(response.status_code, 400)
                self.assertIn('media', response.data)

        response = self.create(media=str(photo.pk), show_channel_picture=True, cpm=5000)
        self.assertEqual(response.data['media'][0], 'Фото канала не показывается вместе с медиа — уберите одно из двух')

        foreign = self.photo(user=make_local_user('stranger'))
        self.assertEqual(str(self.create(media=str(foreign.pk), cpm=5000).data['media'][0]), 'Медиа не найдено')

        processing = AdMedia.objects.create(user=self.user, media_type=Type.VIDEO, status=Status.PROCESSING,
                                            file=ContentFile(b'x', name='p.mp4'))
        self.assertIn('обрабатывается', str(self.create(media=str(processing.pk), cpm=5000).data['media']))
        failed = AdMedia.objects.create(user=self.user, media_type=Type.VIDEO, status=Status.FAILED,
                                        error=ad_media.ERR_RATIO, file=ContentFile(b'x', name='f.mp4'))
        self.assertIn(ad_media.ERR_RATIO, str(self.create(media=str(failed.pk), cpm=5000).data['media']))

    def test_markups(self):
        """Одна наценка: видео → фото (specific при конкретных каналах) → фото канала → 0. База 1000"""
        photo, video = self.photo(), self.ready_video()
        by_languages, by_channels = {'languages': ['uz']}, {'channels': [{'username': 'kunuz', 'id': KUNUZ_ID}]}
        pricing = AdPricing.load()
        for placement, targeting, picture, media, expected in (
            (Placement.CHANNEL_MESSAGE, by_languages, False, None, 1000),
            (Placement.CHANNEL_MESSAGE, by_languages, True, None, 1300),
            (Placement.CHANNEL_MESSAGE, by_languages, False, photo, 1500),
            (Placement.CHANNEL_MESSAGE, by_languages, False, video, 1700),
            (Placement.CHANNEL_MESSAGE, by_channels, False, photo, 1800),
            (Placement.CHANNEL_MESSAGE, by_channels, False, video, 2000),
            (Placement.CHANNEL_MESSAGE, by_channels, True, None, 1300),  # specific — только у медиа
            (Placement.USERS_CHANNEL_MESSAGE, {'countries': ['UZ']}, False, photo, 1500),
            (Placement.USERS_CHANNEL_MESSAGE, {'countries': ['UZ']}, False, video, 1700),
        ):
            with self.subTest(targeting=targeting, picture=picture, media=media and media.media_type):
                self.assertEqual(pricing.min_cpm_for(placement, targeting, picture, media), expected)

        # Через API, и наценки меняются в админке без правки кода
        response = self.create(targeting={'channels': ['kunuz']}, media=str(video.pk), cpm=1999)
        self.assertEqual(response.data['cpm'][0], "CPM can't be less than 2000")
        AdPricing.objects.filter(pk=1).update(video_specific_markup_percent=Decimal('25'))
        self.assertEqual(self.create(targeting={'channels': ['kunuz']}, media=str(video.pk), cpm=1250).status_code,
                         201)

    def test_pricing_endpoint_shows_new_markups(self):
        data = self.client.get('/api/ads/pricing/').data
        self.assertEqual((Decimal(data['photo_specific_markup_percent']), Decimal(data['video_specific_markup_percent'])),
                         (Decimal('80'), Decimal('100')))


class MediaRevisionTests(MediaBase):
    """Медиа меняется только через ревизию, как остальной креатив"""

    def setUp(self):
        super().setUp()
        self.moderator = make_local_user('moder')
        response = create_ad(self.client, Placement.CHANNEL_MESSAGE, targeting={'languages': ['uz']}, cpm=1700)
        self.ad = Ad.objects.get(pk=response.data['id'])

    def approve_ad(self):
        with transaction.atomic():
            Ad.objects.select_for_update().get(pk=self.ad.pk).approve(self.moderator)

    def approve_revision(self):
        with transaction.atomic():
            ad, revision = AdRevision.lock_with_ad(self.ad.revisions.get(status=AdRevision.Status.PENDING).pk)
            revision.approve(ad, self.moderator)

    def patch(self, data):
        return self.client.patch(f'/api/ads/{self.ad.pk}/', data, format='json')

    def test_add_and_remove_media_via_revision(self):
        photo = self.photo()
        self.approve_ad()
        response = self.patch({'media': str(photo.pk)})
        self.assertEqual(response.status_code, 202, response.data)
        self.assertIsNone(response.data['media'])  # крутится прежний вариант
        self.assertEqual(response.data['pending_revision']['media']['id'], str(photo.pk))
        self.approve_revision()
        self.assertEqual(Ad.objects.get(pk=self.ad.pk).media, photo)

        self.assertEqual(self.patch({'media': None}).status_code, 202)  # убрать медиа — тоже через ревизию
        self.assertEqual(Ad.objects.get(pk=self.ad.pk).media, photo)
        self.approve_revision()
        self.assertIsNone(Ad.objects.get(pk=self.ad.pk).media)

    def test_in_review_media_applied_at_once(self):
        photo = self.photo()
        self.assertEqual(self.patch({'media': str(photo.pk)}).status_code, 200)
        self.assertEqual(Ad.objects.get(pk=self.ad.pk).media, photo)

    def test_rules_and_strict_cpm(self):
        video = self.ready_video()
        self.approve_ad()
        self.assertIn('media', self.patch({'media': str(video.pk), 'show_channel_picture': True}).data)
        self.assertEqual(self.patch({'media': str(video.pk)}).status_code, 202)  # видео: минимум 1700
        # Пока правка с видео на проверке, опустить CPM ниже минимума с видео нельзя
        self.assertEqual(self.patch({'cpm': 1500}).data['cpm'][0], "CPM can't be less than 1700")
        # Фото канала при висящей правке с видео — нельзя (правка собирается поверх висящей)
        self.assertIn('media', self.patch({'show_channel_picture': True}).data)

    def test_moderator_sees_media_side_by_side(self):
        photo = self.photo()
        self.approve_ad()
        self.patch({'media': str(photo.pk)})
        User.objects.filter(pk=self.moderator.pk).update(role=User.Role.MODERATOR)
        revision = client_for(User.objects.get(pk=self.moderator.pk)).get(
            '/api/moderation/ads/revisions/pending/').data['results'][0]
        self.assertIsNone(revision['current']['media'])
        self.assertEqual(revision['proposed']['media']['id'], str(photo.pk))


class CpmRepriceTests(MediaBase):
    """
    CPM меняется вместе с наценкой креатива, когда тот вступает в силу (как в Telegram Ads):
    cpm × (100 + новая %) / (100 + старая %) — вверх при росте, вниз при падении, не ниже минимума.
    Цены по умолчанию: база 1000, фото 50 %, видео 70 %, фото канала 30 %, фото / видео при
    конкретных каналах 80 % / 100 %
    """

    def setUp(self):
        super().setUp()
        self.moderator = make_local_user('moder')
        self.ad = self.new_ad(cpm=1001)

    def new_ad(self, **overrides):
        overrides.setdefault('targeting', {'languages': ['uz']})
        response = create_ad(self.client, Placement.CHANNEL_MESSAGE, **overrides)
        self.assertEqual(response.status_code, 201, response.data)
        return Ad.objects.get(pk=response.data['id'])

    def patch(self, data, status=200):
        response = self.client.patch(f'/api/ads/{self.ad.pk}/', data, format='json')
        self.assertEqual(response.status_code, status, response.data)
        return response

    def cpm(self):
        return Ad.objects.get(pk=self.ad.pk).cpm

    def approve_ad(self):
        with transaction.atomic():
            Ad.objects.select_for_update().get(pk=self.ad.pk).approve(self.moderator)

    def decide_revision(self, approve=True):
        with transaction.atomic():
            ad, revision = AdRevision.lock_with_ad(self.ad.revisions.get(status=AdRevision.Status.PENDING).pk)
            if approve:
                revision.approve(ad, self.moderator)
            else:
                revision.decline(ad, self.moderator, 'Не подходит')

    def test_photo_added_and_removed_directly(self):
        # Объявление ещё на проверке — креатив применяется сразу, CPM — тоже
        photo = self.photo()
        self.assertEqual(self.patch({'media': str(photo.pk)}).data['cpm'], 1502)  # 1001 × 1.5 = 1501.5 → вверх
        self.assertEqual(self.patch({'media': None}).data['cpm'], 1001)           # 1502 / 1.5 = 1001.33 → вниз
        self.assertEqual(self.patch({'show_channel_picture': True}).data['cpm'], 1302)  # 1301.3 → вверх
        self.assertEqual(self.patch({'show_channel_picture': False}).data['cpm'], 1001)

    def test_photo_added_and_removed_via_moderation(self):
        photo = self.photo()
        self.approve_ad()
        response = self.patch({'media': str(photo.pk)}, status=202)
        self.assertEqual(response.data['cpm'], 1001)  # пока правка на проверке — крутится прежний вариант
        self.decide_revision()
        self.assertEqual(self.cpm(), 1502)

        self.patch({'media': None}, status=202)
        self.assertEqual(self.cpm(), 1502)
        self.decide_revision()
        self.assertEqual(self.cpm(), 1001)

        self.patch({'show_channel_picture': True}, status=202)
        self.decide_revision()
        self.assertEqual(self.cpm(), 1302)
        self.patch({'show_channel_picture': False}, status=202)
        self.decide_revision()
        self.assertEqual(self.cpm(), 1001)

    def test_declined_revision_keeps_cpm(self):
        photo = self.photo()
        self.approve_ad()
        self.patch({'media': str(photo.pk)}, status=202)
        self.decide_revision(approve=False)
        ad = Ad.objects.get(pk=self.ad.pk)
        self.assertEqual((ad.cpm, ad.media), (1001, None))

    def test_explicit_cpm_checked_as_before(self):
        photo = self.photo()
        # Сразу (на проверке): cpm прислан вместе с фото — минимум с фото, без пересчёта
        response = self.patch({'media': str(photo.pk), 'cpm': 1400}, status=400)
        self.assertEqual(response.data['cpm'][0], "CPM can't be less than 1500")
        self.assertEqual(self.patch({'media': str(photo.pk), 'cpm': 1600}).data['cpm'], 1600)
        self.assertEqual(self.patch({'cpm': 1499}, status=400).data['cpm'][0], "CPM can't be less than 1500")

        # Через модерацию: cpm применяется сразу, при одобрении — пересчёт от CPM на этот момент
        self.ad = self.new_ad(cpm=1000)
        self.approve_ad()
        response = self.patch({'media': str(photo.pk), 'cpm': 1400}, status=400)
        self.assertEqual(response.data['cpm'][0], "CPM can't be less than 1500")
        self.assertEqual(self.patch({'media': str(photo.pk), 'cpm': 1600}, status=202).data['cpm'], 1600)
        self.decide_revision()
        self.assertEqual(self.cpm(), 2400)

    def test_markup_changes_and_minimum(self):
        photo, video = self.photo(), self.ready_video()
        self.assertEqual(self.patch({'text': 'Новый текст'}).data['cpm'], 1001)  # наценка та же — CPM тот же
        self.assertEqual(self.patch({'media': str(photo.pk)}).data['cpm'], 1502)
        self.assertEqual(self.patch({'media': str(self.photo().pk)}).data['cpm'], 1502)  # фото → фото
        self.assertEqual(self.patch({'media': str(video.pk)}).data['cpm'], 1703)  # 1502 × 170 / 150 = 1702.27
        self.assertEqual(self.patch({'media': None}).data['cpm'], 1001)          # 1703 / 1.7 = 1001.76

        # Не ниже минимума нового креатива: суперадмин поднял базовый CPM, пока креатив был с фото канала
        self.assertEqual(self.patch({'show_channel_picture': True}).data['cpm'], 1302)
        AdPricing.objects.filter(pk=1).update(min_cpm_channel_message=1200)
        self.assertEqual(self.patch({'text': 'Ещё текст'}).data['cpm'], 1302)  # наценка та же — не трогаем
        self.assertEqual(self.patch({'show_channel_picture': False}).data['cpm'], 1200)  # 1001 → минимум 1200

    def test_specific_channels_markup(self):
        # Конкретные каналы: фото — 80 %
        self.ad = self.new_ad(cpm=1000, targeting={'channels': ['kunuz']})
        photo = self.photo()
        self.assertEqual(self.patch({'media': str(photo.pk)}).data['cpm'], 1800)
        self.assertEqual(self.patch({'media': None}).data['cpm'], 1000)


class CleanupTests(MediaBase):
    def old(self, media, days=2):
        AdMedia.objects.filter(pk=media.pk).update(created_at=timezone.now() - timedelta(days=days))
        return media

    def test_cleanup_keeps_used_files(self):
        orphan = self.old(self.photo())
        used_by_ad = self.old(self.photo())
        used_by_revision = self.old(self.photo())
        fresh = self.photo()

        ad = Ad.objects.get(pk=create_ad(self.client, Placement.CHANNEL_MESSAGE, targeting={'languages': ['uz']},
                                         media=str(used_by_ad.pk), cpm=1500).data['id'])
        AdRevision.objects.create(ad=ad, media=used_by_revision, status=AdRevision.Status.SUPERSEDED)
        orphan_path = orphan.file.path

        self.assertEqual(cleanup_ad_media(), 1)
        self.assertFalse(AdMedia.objects.filter(pk=orphan.pk).exists())
        self.assertFalse(os.path.exists(orphan_path))  # и файл удалён
        for media in (used_by_ad, used_by_revision, fresh):
            self.assertTrue(AdMedia.objects.filter(pk=media.pk).exists())
            self.assertTrue(os.path.exists(media.file.path))


class AvatarNameTests(TempMediaMixin, TestCase):
    def test_avatar_name_has_content_hash(self):
        peer = TelegramPeer.objects.create(username='kunuz', fetched_at=timezone.now())
        store_avatar(peer, (b'\xff\xd8\xff' + b'1' * 50, 'jpg'))
        first = peer.avatar.name
        store_avatar(peer, (b'\xff\xd8\xff' + b'2' * 50, 'jpg'))
        self.assertRegex(first, r'^tg_avatars/kunuz_[0-9a-f]{8}\.jpg$')
        self.assertNotEqual(first, peer.avatar.name)  # новая картинка — новый URL (можно кэшировать навсегда)
