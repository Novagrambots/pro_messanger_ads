from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import jwt
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.account.authentication import check_adgram_secret
from apps.account.services import create_user_with_balance
from apps.billing.models import Balance

User = get_user_model()

ADGRAM_SECRET = 'test-adgram-secret'
ADGRAM_USER_ID = 'clxadgramuser0001'


def make_adgram_token(user_id=ADGRAM_USER_ID, role='USER', secret=ADGRAM_SECRET,
                      expires_in=timedelta(minutes=15), with_exp=True, username='adgram_name', aud='novagram'):
    """Токен как у AdGram для NovaGram: HS256, payload {userId, username, role, aud: novagram}, 15 минут"""
    now = datetime.now(timezone.utc)
    payload = {'username': username, 'role': role, 'iat': now}
    if user_id is not None:
        payload['userId'] = user_id
    if aud is not None:
        payload['aud'] = aud
    if with_exp:
        payload['exp'] = now + expires_in
    return jwt.encode(payload, secret, algorithm='HS256')


def adgram_client(token):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
    return client


class RegistrationAndRoleTests(TestCase):
    """Регистрация: каждый новый пользователь — рекламодатель, роль снаружи не задать"""

    def test_registration_creates_advertiser_with_balance(self):
        client = APIClient()
        response = client.post('/api/auth/register/', {
            'username': 'newuser',
            'password': 'Str0ngPass!23',
            'password2': 'Str0ngPass!23',
            'email': 'new@test.uz',
            # Пытаемся протащить роль — она должна быть проигнорирована
            'role': 'MODERATOR',
        }, format='json')
        self.assertEqual(response.status_code, 201)

        user = User.objects.get(username='newuser')
        self.assertEqual(user.role, User.Role.ADVERTISER)
        self.assertTrue(Balance.objects.filter(user=user).exists())

    def test_login_returns_role(self):
        User.objects.create_user('someuser', password='Str0ngPass!23')

        client = APIClient()
        response = client.post('/api/auth/login/', {
            'username': 'someuser',
            'password': 'Str0ngPass!23',
        }, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertIn('access', response.data)
        self.assertEqual(response.data['role'], 'ADVERTISER')

    def test_profile_returns_role(self):
        user = User.objects.create_user('someuser', password='pass12345')
        client = APIClient()
        client.force_authenticate(user)

        response = client.get('/api/auth/profile/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['role'], 'ADVERTISER')
        self.assertNotIn('is_admin', response.data)

    def test_superadmin_sees_admin_role_in_login_and_profile(self):
        """Суперадмин в role получает ADMIN — так фронт понимает, что это админка"""
        User.objects.create_superuser('boss', 'boss@test.uz', 'Str0ngPass!23')

        client = APIClient()
        login = client.post('/api/auth/login/', {
            'username': 'boss',
            'password': 'Str0ngPass!23',
        }, format='json')
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.data['role'], 'ADMIN')

        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        profile = client.get('/api/auth/profile/')
        self.assertEqual(profile.status_code, 200)
        self.assertEqual(profile.data['role'], 'ADMIN')

    def test_superadmin_assigns_moderator(self):
        superadmin = User.objects.create_superuser('boss', 'boss@test.uz', 'pass12345')
        user = User.objects.create_user('future_mod', password='pass12345')

        client = APIClient()
        client.force_authenticate(superadmin)
        response = client.post(f'/api/auth/users/{user.user_id}/role/',
                               {'role': 'MODERATOR'}, format='json')
        self.assertEqual(response.status_code, 200)

        user.refresh_from_db()
        self.assertEqual(user.role, User.Role.MODERATOR)
        self.assertTrue(user.is_moderator)

    def test_role_change_rejects_bad_role(self):
        superadmin = User.objects.create_superuser('boss', 'boss@test.uz', 'pass12345')
        user = User.objects.create_user('someuser', password='pass12345')

        client = APIClient()
        client.force_authenticate(superadmin)
        response = client.post(f'/api/auth/users/{user.user_id}/role/',
                               {'role': 'SUPERMAN'}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_registration_rejects_adgram_prefix(self):
        """Имена ag_... зарезервированы под юзеров AdGram (в любом регистре)"""
        client = APIClient()
        for username in ('ag_clxadgramuser0001', 'AG_someone'):
            response = client.post('/api/auth/register/', {
                'username': username,
                'password': 'Str0ngPass!23',
                'password2': 'Str0ngPass!23',
            }, format='json')
            self.assertEqual(response.status_code, 400)
            self.assertIn('username', response.data)
        self.assertFalse(User.objects.filter(username__istartswith='ag_').exists())


@override_settings(ADGRAM_JWT_SECRET=ADGRAM_SECRET)
class AdGramAuthTests(TestCase):
    """Вход по токену AdGram: проверка подписи общим секретом, без запросов в AdGram"""

    def test_valid_token_creates_user_with_balance(self):
        response = adgram_client(make_adgram_token()).get('/api/auth/profile/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['adgram_user_id'], ADGRAM_USER_ID)
        self.assertEqual(response.data['username'], f'ag_{ADGRAM_USER_ID}')

        user = User.objects.get(adgram_user_id=ADGRAM_USER_ID)
        self.assertEqual(user.role, User.Role.ADVERTISER)
        self.assertFalse(user.has_usable_password())
        self.assertEqual(Balance.objects.get(user=user).amount, 0)

    def test_repeat_request_returns_same_user(self):
        first = adgram_client(make_adgram_token()).get('/api/auth/profile/')
        second = adgram_client(make_adgram_token()).get('/api/auth/profile/')
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.data['user_id'], second.data['user_id'])
        self.assertEqual(User.objects.filter(adgram_user_id=ADGRAM_USER_ID).count(), 1)
        self.assertEqual(Balance.objects.filter(user__adgram_user_id=ADGRAM_USER_ID).count(), 1)

    def test_foreign_secret_rejected(self):
        """Подпись не AdGram → токен уходит в simplejwt, тот его тоже не принимает → 401"""
        response = adgram_client(make_adgram_token(secret='other-secret')).get('/api/auth/profile/')
        self.assertEqual(response.status_code, 401)
        self.assertFalse(User.objects.filter(adgram_user_id=ADGRAM_USER_ID).exists())

    def test_expired_token_rejected(self):
        token = make_adgram_token(expires_in=timedelta(minutes=-1))
        response = adgram_client(token).get('/api/auth/profile/')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data['detail'], 'Срок токена AdGram истёк')  # отказал именно наш класс
        self.assertFalse(User.objects.filter(adgram_user_id=ADGRAM_USER_ID).exists())

    def test_token_without_exp_rejected(self):
        response = adgram_client(make_adgram_token(with_exp=False)).get('/api/auth/profile/')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data['detail'], 'Некорректный токен AdGram')
        self.assertFalse(User.objects.filter(adgram_user_id=ADGRAM_USER_ID).exists())

    def test_token_without_user_id_rejected(self):
        response = adgram_client(make_adgram_token(user_id=None)).get('/api/auth/profile/')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data['detail'], 'В токене AdGram нет корректного userId')
        self.assertFalse(User.objects.exclude(adgram_user_id=None).exists())

    def test_token_without_aud_rejected(self):
        """Основной 7-дневный токен AdGram (без aud) в new_dvs не годится"""
        token = make_adgram_token(aud=None, expires_in=timedelta(days=7))
        response = adgram_client(token).get('/api/auth/profile/')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data['detail'], 'Некорректный токен AdGram')
        self.assertFalse(User.objects.filter(adgram_user_id=ADGRAM_USER_ID).exists())

    def test_foreign_aud_rejected(self):
        response = adgram_client(make_adgram_token(aud='adgram')).get('/api/auth/profile/')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data['detail'], 'Некорректный токен AdGram')
        self.assertFalse(User.objects.filter(adgram_user_id=ADGRAM_USER_ID).exists())

    def test_adgram_admin_is_superadmin(self):
        """role=ADMIN в AdGram = суперадмин: модерация и эндпоинты суперадмина, но не Django-админка"""
        client = adgram_client(make_adgram_token(role='ADMIN'))
        profile = client.get('/api/auth/profile/')
        self.assertEqual(profile.status_code, 200)
        self.assertEqual(profile.data['role'], 'ADMIN')

        user = User.objects.get(adgram_user_id=ADGRAM_USER_ID)
        self.assertTrue(user.is_superuser)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.has_usable_password())
        self.assertEqual(user.role, User.Role.ADVERTISER)  # role токен не трогает
        self.assertEqual(client.get('/api/moderation/pending_count/').status_code, 200)
        self.assertEqual(client.get('/api/auth/users/').status_code, 200)  # IsSuperAdmin

    def test_admin_removed_in_adgram_loses_superadmin(self):
        adgram_client(make_adgram_token(role='ADMIN')).get('/api/auth/profile/')
        client = adgram_client(make_adgram_token(role='USER'))
        self.assertEqual(client.get('/api/auth/profile/').data['role'], 'ADVERTISER')
        self.assertFalse(User.objects.get(adgram_user_id=ADGRAM_USER_ID).is_superuser)
        self.assertEqual(client.get('/api/moderation/pending_count/').status_code, 403)
        self.assertEqual(client.get('/api/auth/users/').status_code, 403)

    def test_manual_superuser_overwritten_by_token(self):
        """Источник правды — AdGram: is_superuser, выставленный руками у ag_…, снимется следующим запросом"""
        adgram_client(make_adgram_token()).get('/api/auth/profile/')
        User.objects.filter(adgram_user_id=ADGRAM_USER_ID).update(is_superuser=True)
        adgram_client(make_adgram_token(role='USER')).get('/api/auth/profile/')
        self.assertFalse(User.objects.get(adgram_user_id=ADGRAM_USER_ID).is_superuser)

    def test_moderator_kept_with_user_token(self):
        """MODERATOR хранит new_dvs: токен с USER его не сбрасывает"""
        adgram_client(make_adgram_token()).get('/api/auth/profile/')
        User.objects.filter(adgram_user_id=ADGRAM_USER_ID).update(role=User.Role.MODERATOR)

        client = adgram_client(make_adgram_token(role='USER'))
        self.assertEqual(client.get('/api/auth/profile/').data['role'], 'MODERATOR')
        self.assertEqual(client.get('/api/moderation/pending_count/').status_code, 200)
        self.assertEqual(client.get('/api/auth/users/').status_code, 403)  # модератор — не суперадмин

        # Сняли админа в AdGram — модератором в new_dvs он остаётся
        adgram_client(make_adgram_token(role='ADMIN')).get('/api/auth/profile/')
        adgram_client(make_adgram_token(role='USER')).get('/api/auth/profile/')
        user = User.objects.get(adgram_user_id=ADGRAM_USER_ID)
        self.assertEqual(user.role, User.Role.MODERATOR)
        self.assertFalse(user.is_superuser)

    def test_own_superuser_not_touched(self):
        """createsuperuser (без adgram_user_id) токенами AdGram не затрагивается"""
        boss = User.objects.create_superuser('boss', password='Str0ngPass!23')
        adgram_client(make_adgram_token(role='USER')).get('/api/auth/profile/')
        adgram_client(make_adgram_token(user_id='clxother', role='ADMIN')).get('/api/auth/profile/')
        boss.refresh_from_db()
        self.assertTrue(boss.is_superuser)
        self.assertTrue(boss.is_staff)

        client = APIClient()
        login = client.post('/api/auth/login/', {'username': 'boss', 'password': 'Str0ngPass!23'}, format='json')
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        self.assertEqual(client.get('/api/auth/users/').status_code, 200)

    def test_same_role_no_extra_update(self):
        """Админство не поменялось — лишнего UPDATE нет (как с именем)"""
        client = adgram_client(make_adgram_token(role='ADMIN'))
        client.get('/api/auth/profile/')
        with patch.object(User.objects, 'filter', wraps=User.objects.filter) as user_filter:
            client.get('/api/auth/profile/')
        self.assertEqual(user_filter.call_count, 1)

    def test_own_login_and_moderator_token_still_work(self):
        """Свой логин new_dvs (simplejwt) работает как раньше, в т.ч. у модератора"""
        User.objects.create_user('someuser', password='Str0ngPass!23')
        User.objects.create_user('moder', password='Str0ngPass!23', role=User.Role.MODERATOR)
        client = APIClient()

        login = client.post('/api/auth/login/', {'username': 'someuser', 'password': 'Str0ngPass!23'},
                            format='json')
        self.assertEqual(login.status_code, 200)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        profile = client.get('/api/auth/profile/')
        self.assertEqual(profile.status_code, 200)
        self.assertEqual(profile.data['username'], 'someuser')
        self.assertIsNone(profile.data['adgram_user_id'])

        client.credentials()
        login = client.post('/api/auth/login/', {'username': 'moder', 'password': 'Str0ngPass!23'},
                            format='json')
        self.assertEqual(login.status_code, 200)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        self.assertEqual(client.get('/api/moderation/pending_count/').status_code, 200)

    @override_settings(ADGRAM_JWT_SECRET='')
    def test_empty_secret_disables_adgram_login(self):
        response = adgram_client(make_adgram_token()).get('/api/auth/profile/')
        self.assertEqual(response.status_code, 401)
        self.assertFalse(User.objects.filter(adgram_user_id=ADGRAM_USER_ID).exists())

    def test_parallel_first_requests_do_not_duplicate(self):
        """Гонка двух первых запросов: второй ловит IntegrityError и берёт уже созданного юзера"""
        def parallel_create(**kwargs):
            create_user_with_balance(**kwargs)  # параллельный запрос успел создать первым
            raise IntegrityError('duplicate key value violates unique constraint')

        with patch('apps.account.services.create_user_with_balance', side_effect=parallel_create):
            response = adgram_client(make_adgram_token()).get('/api/auth/profile/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(User.objects.filter(adgram_user_id=ADGRAM_USER_ID).count(), 1)

    def test_taken_username_gives_401_not_500(self):
        """Если имя ag_<userId> уже занято (старый юзер), отвечаем 401, а не падаем"""
        User.objects.create_user(f'ag_{ADGRAM_USER_ID}', password='pass12345')
        response = adgram_client(make_adgram_token()).get('/api/auth/profile/')
        self.assertEqual(response.status_code, 401)
        self.assertFalse(User.objects.filter(adgram_user_id=ADGRAM_USER_ID).exists())

    def test_inactive_user_rejected(self):
        adgram_client(make_adgram_token()).get('/api/auth/profile/')
        User.objects.filter(adgram_user_id=ADGRAM_USER_ID).update(is_active=False)

        response = adgram_client(make_adgram_token()).get('/api/auth/profile/')
        self.assertEqual(response.status_code, 401)

    def test_adgram_username_saved_and_updated(self):
        """Имя из AdGram — только для отображения: пишем при создании, обновляем, если поменяли"""
        adgram_client(make_adgram_token(username='Ali')).get('/api/auth/profile/')
        user = User.objects.get(adgram_user_id=ADGRAM_USER_ID)
        self.assertEqual(user.adgram_username, 'Ali')

        adgram_client(make_adgram_token(username='Ali Valiyev')).get('/api/auth/profile/')
        user.refresh_from_db()
        self.assertEqual(user.adgram_username, 'Ali Valiyev')

        # Не строка — имя не трогаем, вход при этом работает
        response = adgram_client(make_adgram_token(username=None)).get('/api/auth/profile/')
        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertEqual(user.adgram_username, 'Ali Valiyev')

    def test_same_adgram_username_no_extra_update(self):
        """Имя не поменялось — лишнего UPDATE на каждый запрос нет"""
        client = adgram_client(make_adgram_token(username='Ali'))
        client.get('/api/auth/profile/')
        with patch.object(User.objects, 'filter', wraps=User.objects.filter) as user_filter:
            client.get('/api/auth/profile/')
        # filter вызывается один раз — поиск юзера; второго (на UPDATE имени) нет
        self.assertEqual(user_filter.call_count, 1)

    def test_no_token_gives_401(self):
        """Первый класс аутентификации отдаёт WWW-Authenticate — иначе DRF ответил бы 403"""
        response = APIClient().get('/api/auth/profile/')
        self.assertEqual(response.status_code, 401)
        self.assertIn('Bearer', response['WWW-Authenticate'])



@override_settings(ADGRAM_JWT_SECRET=ADGRAM_SECRET)
class AdGramUserRolesTests(TestCase):
    """Модераторы назначаются с сайта AdGram: API суперадмина по adgram_user_id"""
    PERSON = 'clxperson0002'

    def setUp(self):
        self.admin = adgram_client(make_adgram_token(user_id='clxadmin0001', role='ADMIN'))

    def put_role(self, role, adgram_user_id=PERSON, client=None):
        return (client or self.admin).put(f'/api/auth/adgram-users/{adgram_user_id}/role/', {'role': role},
                                          format='json')

    def get_role(self, adgram_user_id=PERSON, client=None):
        return (client or self.admin).get(f'/api/auth/adgram-users/{adgram_user_id}/')

    def test_unknown_person_is_advertiser_without_shadow(self):
        response = self.get_role()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data, {'adgram_user_id': self.PERSON, 'adgram_username': '',
                                         'role': 'ADVERTISER', 'user_id': None})
        self.assertFalse(User.objects.filter(adgram_user_id=self.PERSON).exists())  # тень не создана

    def test_assign_creates_shadow_and_login_picks_it_up(self):
        response = self.put_role('MODERATOR')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['role'], 'MODERATOR')

        shadow = User.objects.get(adgram_user_id=self.PERSON)
        self.assertEqual(shadow.username, f'ag_{self.PERSON}')
        self.assertEqual(shadow.adgram_username, '')  # заполнится при первом входе
        self.assertFalse(shadow.has_usable_password())
        self.assertFalse(shadow.is_superuser)
        self.assertTrue(Balance.objects.filter(user=shadow).exists())
        self.assertEqual(response.data['user_id'], str(shadow.user_id))
        self.assertEqual(self.get_role().data['role'], 'MODERATOR')

        # Человек впервые входит через AdGram — тот же пользователь, уже модератор, имя подтянулось
        person = adgram_client(make_adgram_token(user_id=self.PERSON, username='Ali'))
        profile = person.get('/api/auth/profile/')
        self.assertEqual(profile.data['role'], 'MODERATOR')  # по профилю AdGram показывает «Moderatsiya»
        self.assertEqual(profile.data['user_id'], str(shadow.user_id))
        self.assertEqual(User.objects.filter(adgram_user_id=self.PERSON).count(), 1)
        self.assertEqual(User.objects.get(adgram_user_id=self.PERSON).adgram_username, 'Ali')
        self.assertEqual(person.get('/api/moderation/pending_count/').status_code, 200)

    def test_revoke_moderator(self):
        self.put_role('MODERATOR')
        person = adgram_client(make_adgram_token(user_id=self.PERSON))
        self.assertEqual(person.get('/api/moderation/pending_count/').status_code, 200)

        response = self.put_role('ADVERTISER')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['role'], 'ADVERTISER')
        self.assertEqual(person.get('/api/auth/profile/').data['role'], 'ADVERTISER')
        self.assertEqual(person.get('/api/moderation/pending_count/').status_code, 403)

    def test_list_by_role(self):
        self.put_role('MODERATOR')
        self.put_role('MODERATOR', adgram_user_id='clxperson0003')
        adgram_client(make_adgram_token(user_id='clxplain0004')).get('/api/auth/profile/')  # просто рекламодатель
        User.objects.create_user('local_moder', password='pass12345', role=User.Role.MODERATOR)  # не из AdGram

        response = self.admin.get('/api/auth/adgram-users/?role=MODERATOR')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['count'], 2)
        self.assertEqual({item['adgram_user_id'] for item in response.data['results']},
                         {self.PERSON, 'clxperson0003'})
        self.assertEqual(set(response.data['results'][0]), {'adgram_user_id', 'adgram_username', 'role', 'user_id'})

        everyone = self.admin.get('/api/auth/adgram-users/?page_size=100').data
        self.assertEqual(everyone['count'], 4)  # + clxplain0004 и сам админ (только юзеры AdGram)
        self.assertEqual(self.admin.get('/api/auth/adgram-users/?role=ADMIN').status_code, 400)

    def test_advertiser_and_moderator_forbidden(self):
        advertiser = adgram_client(make_adgram_token(user_id='clxplain0004'))
        self.put_role('MODERATOR', adgram_user_id='clxmoder0005')
        moderator = adgram_client(make_adgram_token(user_id='clxmoder0005'))
        for client in (advertiser, moderator):
            self.assertEqual(client.get('/api/auth/adgram-users/').status_code, 403)
            self.assertEqual(self.get_role(client=client).status_code, 403)
            self.assertEqual(self.put_role('MODERATOR', client=client).status_code, 403)
        self.assertFalse(User.objects.filter(adgram_user_id=self.PERSON).exists())

    def test_django_superuser_can_too(self):
        boss = User.objects.create_superuser('boss', password='Str0ngPass!23')
        client = APIClient()
        client.force_authenticate(boss)
        self.assertEqual(self.put_role('MODERATOR', client=client).status_code, 200)

    def test_bad_adgram_user_id_400(self):
        for bad in ('bad id', 'x' * 65, 'имя', 'a.b'):
            with self.subTest(bad=bad):
                self.assertEqual(self.put_role('MODERATOR', adgram_user_id=bad).status_code, 400)
                self.assertEqual(self.get_role(adgram_user_id=bad).status_code, 400)
        self.assertEqual(User.objects.count(), 1)  # только сам админ

    def test_admin_role_or_garbage_400(self):
        for role in ('ADMIN', 'moderator', '', None):
            with self.subTest(role=role):
                response = self.put_role(role)
                self.assertEqual(response.status_code, 400)
                self.assertIn('role', response.data)
        self.assertFalse(User.objects.filter(adgram_user_id=self.PERSON).exists())

    def test_admin_from_token_keeps_moderator_role_separately(self):
        """ADMIN AdGram с назначенным MODERATOR: здесь видна роль new_dvs, в профиле — ADMIN"""
        self.put_role('MODERATOR', adgram_user_id='clxadmin0001')
        self.assertEqual(self.get_role(adgram_user_id='clxadmin0001').data['role'], 'MODERATOR')
        self.assertEqual(self.admin.get('/api/auth/profile/').data['role'], 'ADMIN')

    def test_taken_username_409(self):
        User.objects.create_user(f'ag_{self.PERSON}', password='pass12345')  # старый юзер с таким именем
        response = self.put_role('MODERATOR')
        self.assertEqual(response.status_code, 409)
        self.assertFalse(User.objects.filter(adgram_user_id=self.PERSON).exists())

class AdGramSecretCheckTests(TestCase):
    """Секрет AdGram, совпавший с SECRET_KEY, роняет старт приложения"""

    def test_secret_equal_to_secret_key_fails(self):
        with override_settings(ADGRAM_JWT_SECRET=settings.SECRET_KEY):
            with self.assertRaises(ImproperlyConfigured):
                check_adgram_secret()

    def test_separate_or_empty_secret_ok(self):
        for secret in ('', ADGRAM_SECRET):
            with override_settings(ADGRAM_JWT_SECRET=secret):
                check_adgram_secret()
