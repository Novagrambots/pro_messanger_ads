"""
Тесты черновика формы и «Create a similar ad» (этап 9): GET / PUT / DELETE черновика,
проверка размера и формата, троттл, удаление после создания объявления, копия объявления
в черновик (и что из неё снова создаётся объявление), очистка медиа не трогает медиа из черновика.
"""
import os
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase
from django.utils import timezone

from apps.ads.models import Ad, AdDraft, AdMedia, AdStatus, InitialStatus, Placement
from apps.ads.tasks import cleanup_ad_media
from apps.ads.tests import (KUNUZ_ID, call_locked, client_for, create_ad, make_local_user, setUpModule,  # noqa: F401
                            tearDownModule)
from apps.directories.models import TelegramPeer
from apps.directories.tests import TempMediaMixin

User = get_user_model()
URL = '/api/ads/draft/'
SCHEDULE = {'enabled': True, 'days': [16777215, 0, 255, 0, 0, 0, 0], 'timezone': 'Asia/Tashkent'}


def copy_url(ad):
    return f'/api/ads/{ad.pk}/copy-to-draft/'


class DraftBase(TempMediaMixin, TestCase):
    """TempMediaMixin: медиа во временную папку и чистый кэш (счётчики троттла) в каждом тесте"""

    def setUp(self):
        super().setUp()
        self.user = make_local_user('owner')
        self.client = client_for(self.user)

    def new_ad(self, placement=Placement.BOT, **overrides):
        response = create_ad(self.client, placement, **overrides)
        self.assertEqual(response.status_code, 201, response.data)
        return Ad.objects.get(pk=response.data['id'])

    def media(self, days_old=0):
        media = AdMedia.objects.create(user=self.user, media_type=AdMedia.MediaType.PHOTO,
                                       status=AdMedia.Status.READY, width=1280, height=720,
                                       file=ContentFile(b'photo', name='photo.jpg'))
        if days_old:
            AdMedia.objects.filter(pk=media.pk).update(created_at=timezone.now() - timedelta(days=days_old))
        return media


class DraftApiTests(DraftBase):
    def test_get_without_draft(self):
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {'data': None, 'updated_at': None})

    def test_put_get_delete(self):
        form = {'placement': 'SEARCH', 'title': 'Недописанное', 'targeting': {'queries': ['нов']}}
        response = self.client.put(URL, {'data': form}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['data'], form)
        self.assertIsNotNone(response.data['updated_at'])

        # Полная замена, а не слияние
        response = self.client.put(URL, {'data': {'title': 'Другое'}}, format='json')
        self.assertEqual(self.client.get(URL).data['data'], {'title': 'Другое'})
        self.assertEqual(AdDraft.objects.filter(user=self.user).count(), 1)

        self.assertEqual(self.client.delete(URL).status_code, 204)
        self.assertEqual(self.client.get(URL).data['data'], None)
        self.assertEqual(self.client.delete(URL).status_code, 204)  # черновика нет — тоже 204

    def test_empty_object_is_ok(self):
        response = self.client.put(URL, {'data': {}}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['data'], {})

    def test_autosave_does_not_check_telegram(self):
        # Автосохранение раз в 3 с: каналы в Telegram не проверяем (сеть в этих тестах запрещена),
        # TelegramPeer не создаём — проверка только при создании объявления
        channels = [f'newchan{i}' for i in range(100)]
        form = {'placement': 'CHANNEL_MESSAGE', 'targeting': {'channels': channels, 'exclude_channels': channels}}
        response = self.client.put(URL, {'data': form}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(TelegramPeer.objects.filter(username__startswith='newchan').exists())

    def test_not_object_400(self):
        for value in ([1, 2], 'текст', 5, None):
            response = self.client.put(URL, {'data': value}, format='json')
            self.assertEqual(response.status_code, 400, value)
            self.assertIn('data', response.data)
        self.assertEqual(self.client.put(URL, {}, format='json').status_code, 400)  # нет data
        self.assertFalse(AdDraft.objects.exists())

    def test_size_limit_64kb(self):
        # Кириллица — 2 байта в UTF-8: считаем байты, а не символы
        ok = {'text': 'я' * (32 * 1024 - 20)}
        self.assertEqual(self.client.put(URL, {'data': ok}, format='json').status_code, 200)
        too_big = {'text': 'я' * (32 * 1024)}
        response = self.client.put(URL, {'data': too_big}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(str(response.data['data'][0]), 'Черновик больше 64 КБ')
        self.assertEqual(AdDraft.objects.get(user=self.user).data, ok)  # прежний черновик не тронут

    def test_draft_is_per_user(self):
        self.client.put(URL, {'data': {'title': 'мой'}}, format='json')
        other = client_for(make_local_user('other'))
        self.assertEqual(other.get(URL).data['data'], None)
        other.delete(URL)
        self.assertEqual(self.client.get(URL).data['data'], {'title': 'мой'})

    def test_throttle_60_per_minute(self):
        for _ in range(60):
            self.assertEqual(self.client.put(URL, {'data': {}}, format='json').status_code, 200)
        self.assertEqual(self.client.get(URL).status_code, 429)
        # У другого пользователя свой счётчик
        self.assertEqual(client_for(make_local_user('other')).get(URL).status_code, 200)

    def test_requires_auth(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(URL).status_code, 401)

    def test_draft_deleted_after_ad_created(self):
        self.client.put(URL, {'data': {'title': 'черновик'}}, format='json')
        self.new_ad()
        self.assertFalse(AdDraft.objects.filter(user=self.user).exists())

    def test_draft_kept_when_create_fails(self):
        self.client.put(URL, {'data': {'title': 'черновик'}}, format='json')
        self.assertEqual(create_ad(self.client, cpm=1).status_code, 400)
        self.assertTrue(AdDraft.objects.filter(user=self.user).exists())


class CopyToDraftTests(DraftBase):
    def copy(self, ad, client=None):
        return (client or self.client).post(copy_url(ad))

    def assert_creates_again(self, data):
        """Копия — в формате формы создания: добавили бюджет и согласие — объявление создаётся"""
        response = self.client.post('/api/ads/', {**data, 'budget': '10000', 'agree_terms': True}, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        return Ad.objects.get(pk=response.data['id'])

    def test_copy_channel_message_all_fields(self):
        media = self.media()
        ad = self.new_ad(
            Placement.CHANNEL_MESSAGE, title='Kun.uz', text='Свежие новости', other_info='erid: 123',
            media=str(media.pk), cpm=3000, daily_limit=5000, views_per_user_per_day=2,
            initial_status=InitialStatus.ON_HOLD, start_date=(timezone.now() + timedelta(days=3)).isoformat(),
            end_date=(timezone.now() + timedelta(days=10)).isoformat(), schedule=SCHEDULE,
            targeting={'channels': ['t.me/daryo']})
        # Свой канал при создании сам попал в исключения
        self.assertEqual(ad.targeting['exclude_channels'], [{'username': 'kunuz', 'id': KUNUZ_ID}])

        response = self.copy(ad)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['data'], {
            'placement': 'CHANNEL_MESSAGE', 'title': 'Kun.uz', 'text': 'Свежие новости',
            'url': 'https://t.me/kunuz', 'show_channel_picture': False, 'other_info': 'erid: 123',
            'cpm': 3000, 'daily_limit': 5000, 'views_per_user_per_day': 2, 'initial_status': 'ON_HOLD',
            'schedule': SCHEDULE, 'media': str(media.pk),
            # Каналы — именами, как их шлёт форма; свой канал из исключений убран
            'targeting': {'languages': [], 'topics': [], 'channels': ['daryo'], 'exclude_topics': [],
                          'exclude_channels': []},
        })
        self.assertIsNotNone(response.data['updated_at'])
        self.assertEqual(AdDraft.objects.get(user=self.user).data, response.data['data'])

        again = self.assert_creates_again(response.data['data'])
        self.assertEqual(again.media_id, media.pk)
        self.assertEqual(again.targeting['exclude_channels'], [{'username': 'kunuz', 'id': KUNUZ_ID}])

    def test_no_money_status_dates_terms(self):
        ad = self.new_ad(Placement.SEARCH, initial_status=InitialStatus.ON_HOLD,
                         end_date=(timezone.now() + timedelta(days=10)).isoformat())
        data = self.copy(ad).data['data']
        for field in ('budget', 'spent', 'refunded', 'status', 'start_date', 'end_date', 'agree_terms',
                      'terms_version', 'terms_accepted_at', 'id', 'created_at'):
            self.assertNotIn(field, data)
        self.assertEqual(data['targeting'], {'queries': ['новости uz']})
        self.assertIsNone(data['media'])
        self.assert_creates_again(data)

    def test_copy_users_channel_audiences(self):
        ad = self.new_ad(Placement.USERS_CHANNEL_MESSAGE, show_channel_picture=True, cpm=2000,
                         targeting={'countries': ['UZ'], 'channel_audiences': ['daryo'],
                                    'exclude_channel_audiences': ['@kunuz'], 'politics': 'exclude'})
        data = self.copy(ad).data['data']
        self.assertTrue(data['show_channel_picture'])
        self.assertEqual(data['targeting']['channel_audiences'], ['daryo'])
        self.assertEqual(data['targeting']['exclude_channel_audiences'], ['kunuz'])
        self.assertEqual(data['targeting']['politics'], 'exclude')
        self.assert_creates_again(data)

    def test_copy_takes_live_creative_not_pending_revision(self):
        ad = self.new_ad(text='Старый текст')
        call_locked(ad, 'approve', User.objects.create_superuser('boss', password='pass12345'))
        response = self.client.patch(f'/api/ads/{ad.pk}/', {'text': 'Новый текст'}, format='json')
        self.assertEqual(response.status_code, 202, response.data)  # правка ушла на модерацию
        self.assertEqual(self.copy(ad).data['data']['text'], 'Старый текст')

    def test_copy_replaces_draft(self):
        ad = self.new_ad(title='Объявление')
        self.client.put(URL, {'data': {'title': 'старый черновик', 'budget': '500'}}, format='json')
        data = self.copy(ad).data['data']
        self.assertEqual(data['title'], 'Объявление')
        self.assertNotIn('budget', data)
        self.assertEqual(AdDraft.objects.filter(user=self.user).count(), 1)

    def test_foreign_and_deleted_404(self):
        ad = self.new_ad()
        other = client_for(make_local_user('other'))
        self.assertEqual(self.copy(ad, other).status_code, 404)
        self.assertFalse(AdDraft.objects.exists())

        Ad.objects.filter(pk=ad.pk).update(status=AdStatus.DELETED)
        self.assertEqual(self.copy(ad).status_code, 404)

    def test_declined_ad_can_be_copied(self):
        ad = self.new_ad(title='Отклонённое')
        call_locked(ad, 'decline', User.objects.create_superuser('boss', password='pass12345'), 'Плохой текст')
        response = self.copy(ad)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['data']['title'], 'Отклонённое')


class DraftMediaCleanupTests(DraftBase):
    def test_cleanup_keeps_media_from_draft(self):
        in_draft = self.media(days_old=2)
        orphan = self.media(days_old=2)
        orphan_path, kept_path = orphan.file.path, in_draft.file.path
        self.client.put(URL, {'data': {'media': str(in_draft.pk)}}, format='json')
        # Мусор в чужом черновике очистку не ломает
        AdDraft.objects.create(user=make_local_user('other'), data={'media': 'не-uuid'})

        self.assertEqual(cleanup_ad_media(), 1)
        self.assertFalse(AdMedia.objects.filter(pk=orphan.pk).exists())
        self.assertFalse(os.path.exists(orphan_path))
        self.assertTrue(AdMedia.objects.filter(pk=in_draft.pk).exists())
        self.assertTrue(os.path.exists(kept_path))

        # Черновик очистили — медиа больше никому не нужно
        self.client.delete(URL)
        self.assertEqual(cleanup_ad_media(), 1)
        self.assertFalse(AdMedia.objects.filter(pk=in_draft.pk).exists())

    def test_copied_ad_media_survives_after_ad_deleted(self):
        """Копия держит медиа, даже если само объявление потом перестанет на него ссылаться"""
        media = self.media(days_old=2)
        ad = self.new_ad(Placement.CHANNEL_MESSAGE, media=str(media.pk), cpm=3000,
                         targeting={'languages': ['uz']})
        self.assertEqual(self.client.post(copy_url(ad)).status_code, 200)
        Ad.objects.filter(pk=ad.pk).update(media=None)  # будто объявление сменило медиа правкой
        self.assertEqual(cleanup_ad_media(), 0)
        self.assertTrue(AdMedia.objects.filter(pk=media.pk).exists())
