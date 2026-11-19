"""
Показ объявлений суперадмина другим пользователям (AdShare): кабинет AdGram видит их в «Моих объявлениях»,
может смотреть статистику и ставить на паузу, но не править, не пополнять и не удалять.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.ads.models import Ad, AdShare, AdStatus, Placement
from apps.ads.tests import client_for, create_ad, make_local_user, setUpModule, tearDownModule  # noqa: F401

User = get_user_model()
ADGRAM_ID = 'cmob6z71v0009lf01feafae8l'


class AdShareTests(TestCase):
    def setUp(self):
        self.boss = make_local_user('boss')
        self.boss.is_superuser = True
        self.boss.save(update_fields=['is_superuser'])
        self.boss_client = client_for(self.boss)
        self.viewer = make_local_user('viewer')
        self.viewer_client = client_for(self.viewer)
        response = create_ad(self.boss_client, Placement.SEARCH)
        self.assertEqual(response.status_code, 201, response.data)
        self.ad_id = response.data['id']

    def share(self, *users, client=None):
        return (client or self.boss_client).put(f'/api/ads/{self.ad_id}/shares/', {'users': list(users)},
                                                format='json')

    def listed_ids(self, client):
        response = client.get('/api/ads/')
        self.assertEqual(response.status_code, 200, response.data)
        return {row['id']: row['shared'] for row in response.data['results']}

    def test_hidden_until_shared(self):
        self.assertEqual(self.listed_ids(self.viewer_client), {})
        self.assertEqual(self.viewer_client.get(f'/api/ads/{self.ad_id}/').status_code, 404)
        self.assertEqual(self.viewer_client.get(f'/api/ads/{self.ad_id}/stats/').status_code, 404)

    def test_shared_ad_is_listed_and_readable(self):
        response = self.share({'user_id': str(self.viewer.user_id)})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([u['username'] for u in response.data['users']], ['viewer'])

        self.assertEqual(self.listed_ids(self.viewer_client), {self.ad_id: True})
        self.assertEqual(self.listed_ids(self.boss_client), {self.ad_id: False})
        self.assertEqual(self.viewer_client.get(f'/api/ads/{self.ad_id}/').status_code, 200)
        self.assertEqual(self.viewer_client.get(f'/api/ads/{self.ad_id}/stats/').status_code, 200)
        self.assertEqual(self.viewer_client.get(f'/api/ads/{self.ad_id}/budget-history/').status_code, 200)

    def test_shared_viewer_can_only_pause_and_resume(self):
        self.share({'user_id': str(self.viewer.user_id)})
        url = f'/api/ads/{self.ad_id}/'

        response = self.viewer_client.patch(url, {'status': 'ON_HOLD'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(Ad.objects.get(pk=self.ad_id).status, AdStatus.ON_HOLD)
        response = self.viewer_client.patch(url, {'status': 'ACTIVE'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(Ad.objects.get(pk=self.ad_id).status, AdStatus.ACTIVE)

        self.assertEqual(self.viewer_client.patch(url, {'title': 'x'}, format='json').status_code, 403)
        self.assertEqual(self.viewer_client.patch(url, {'status': 'ON_HOLD', 'cpm': 5}, format='json').status_code,
                         403)
        self.assertEqual(self.viewer_client.patch(url, {}, format='json').status_code, 403)
        self.assertEqual(self.viewer_client.delete(url).status_code, 404)
        self.assertEqual(self.viewer_client.post(f'{url}budget/', {'amount': '10000'}, format='json').status_code,
                         404)
        self.assertEqual(self.viewer_client.post(f'{url}copy-to-draft/').status_code, 404)
        self.assertEqual(Ad.objects.get(pk=self.ad_id).status, AdStatus.ACTIVE)

    def test_only_superuser_can_share(self):
        self.assertEqual(self.share({'user_id': str(self.viewer.user_id)}, client=self.viewer_client).status_code, 403)
        self.assertEqual(self.viewer_client.get(f'/api/ads/{self.ad_id}/shares/').status_code, 403)
        response = self.viewer_client.post('/api/ads/shares/', {'all_own': True, 'add': []}, format='json')
        self.assertEqual(response.status_code, 403)

    def test_share_with_adgram_user_creates_shadow_user(self):
        response = self.share({'adgram_user_id': ADGRAM_ID, 'adgram_username': 'omonulloxo9575'})
        self.assertEqual(response.status_code, 200, response.data)
        shadow = User.objects.get(adgram_user_id=ADGRAM_ID)
        self.assertEqual(response.data['users'][0]['adgram_user_id'], ADGRAM_ID)
        self.assertEqual(self.listed_ids(client_for(shadow)), {self.ad_id: True})
        # Повторный показ тому же юзеру AdGram — тот же пользователь, без дублей
        self.share({'adgram_user_id': ADGRAM_ID, 'adgram_username': 'omonulloxo9575'})
        self.assertEqual(User.objects.filter(adgram_user_id=ADGRAM_ID).count(), 1)
        self.assertEqual(AdShare.objects.filter(ad_id=self.ad_id).count(), 1)

    def test_bad_adgram_id_is_rejected(self):
        self.assertEqual(self.share({'adgram_user_id': 'bad id!'}).status_code, 400)
        self.assertEqual(self.share({}).status_code, 400)

    def test_replace_list_and_hide_again(self):
        self.share({'user_id': str(self.viewer.user_id)})
        response = self.share()
        self.assertEqual(response.data['users'], [])
        self.assertEqual(self.listed_ids(self.viewer_client), {})

    def test_owner_is_never_added_to_own_ad(self):
        self.share({'user_id': str(self.boss.user_id)})
        self.assertFalse(AdShare.objects.filter(ad_id=self.ad_id).exists())

    def test_bulk_all_own(self):
        second = create_ad(self.boss_client, Placement.SEARCH).data['id']
        stranger = make_local_user('stranger')
        foreign = create_ad(client_for(stranger), Placement.SEARCH).data['id']
        add = {'all_own': True, 'add': [{'user_id': str(self.viewer.user_id)}]}

        response = self.boss_client.post('/api/ads/shares/', add, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data, {'ads': 2, 'added': 2, 'removed': 0})
        self.assertEqual(set(self.listed_ids(self.viewer_client)), {self.ad_id, second})
        self.assertNotIn(foreign, self.listed_ids(self.viewer_client))

        self.assertEqual(self.boss_client.post('/api/ads/shares/', add, format='json').data['added'], 0)
        response = self.boss_client.post('/api/ads/shares/', {
            'ad_ids': [self.ad_id], 'remove': [{'user_id': str(self.viewer.user_id)}]}, format='json')
        self.assertEqual(response.data, {'ads': 1, 'added': 0, 'removed': 1})
        self.assertEqual(set(self.listed_ids(self.viewer_client)), {second})

    def test_moderation_list_shows_shared_with(self):
        self.share({'user_id': str(self.viewer.user_id)})
        response = self.boss_client.get('/api/moderation/ads/all/')
        self.assertEqual(response.status_code, 200, response.data)
        row = next(r for r in response.data['results'] if r['id'] == self.ad_id)
        self.assertEqual([u['username'] for u in row['shared_with']], ['viewer'])
        self.assertFalse(row['shared'])
