"""
Тесты модерации объявлений v2: очередь и фильтры, решения с точным возвратом, права,
автоодобрение суперадмина, ревизии креатива, уведомления, гонка «одобрение правки ∥ новая правка».
"""
import threading
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings

from apps.account.services import create_user_with_balance
from apps.ads.models import Ad, AdDecision, AdReport, AdRevision, AdStateError, AdStatus, LabelStyle, Placement
from apps.ads.tests import (ADGRAM_SETTINGS, SPORTS, client_for, create_ad, make_local_user,  # noqa: F401
                            setUpModule, tearDownModule)
from apps.billing.adgram_client import OperationResult
from apps.billing.models import Balance, MoneyOperation as Op
from apps.notifications.models import Notification
from apps.partner.models import ChannelEarning

User = get_user_model()
Rev = AdRevision.Status


def make_moderator(username='moder'):
    user = make_local_user(username)
    User.objects.filter(pk=user.pk).update(role=User.Role.MODERATOR)
    user.refresh_from_db()
    return user


class ModerationBase(TestCase):
    def setUp(self):
        # Push в тестах не шлём (иначе Celery стучится в Redis)
        push = patch('apps.notifications.services._enqueue_push')
        push.start()
        self.addCleanup(push.stop)

        self.owner = make_local_user('owner')
        self.moderator = make_moderator()
        self.boss = User.objects.create_superuser('boss', 'boss@test.uz', 'pass12345')
        Balance.objects.create(user=self.boss, amount=Decimal('1000000'))
        self.owner_client = client_for(self.owner)
        self.mod_client = client_for(self.moderator)

    def new_ad(self, client=None, **overrides):
        response = create_ad(client or self.owner_client, **overrides)
        self.assertEqual(response.status_code, 201, response.data)
        return Ad.objects.get(pk=response.data['id'])

    def mod(self, path, data=None, client=None):
        return (client or self.mod_client).post(f'/api/moderation/{path}', data or {}, format='json')

    def approved_ad(self, **overrides):
        ad = self.new_ad(**overrides)
        self.assertEqual(self.mod(f'ads/{ad.pk}/approve/').status_code, 200)
        return Ad.objects.get(pk=ad.pk)

    def patch_ad(self, ad, data):
        return self.owner_client.patch(f'/api/ads/{ad.pk}/', data, format='json')

    def refresh(self, obj):
        return type(obj).objects.get(pk=obj.pk)


class AdDecisionTests(ModerationBase):
    """Очередь, решения по объявлениям, права, уведомления"""

    def test_queue_filters_and_card(self):
        first = self.new_ad(placement=Placement.BOT)
        second = self.new_ad(placement=Placement.SEARCH)
        approved = self.approved_ad()
        deleted = self.new_ad()
        self.owner_client.delete(f'/api/ads/{deleted.pk}/')

        pending = self.mod_client.get('/api/moderation/ads/pending/').data['results']
        self.assertEqual([item['id'] for item in pending], [str(first.pk), str(second.pk)])  # старые сверху

        def ids(query):
            return {item['id'] for item in self.mod_client.get(f'/api/moderation/ads/all/{query}').data['results']}

        self.assertEqual(ids(''), {str(first.pk), str(second.pk), str(approved.pk)})  # без удалённых
        self.assertEqual(ids('?status=ACTIVE'), {str(approved.pk)})
        self.assertEqual(ids('?placement=SEARCH'), {str(second.pk)})

        User.objects.filter(pk=self.owner.pk).update(adgram_username='Ali Valiyev')
        card = self.mod_client.get(f'/api/moderation/ads/{approved.pk}/').data
        self.assertEqual((card['owner']['username'], card['owner']['adgram_username']), ('owner', 'Ali Valiyev'))
        self.assertEqual(card['remaining_budget'], '10000.00')
        self.assertEqual(card['targeting'], {'bots': ['weatherbot']})
        self.assertEqual([d['action'] for d in card['decisions']], ['APPROVED'])
        self.assertEqual(card['decisions'][0]['moderator'], 'moder')
        # Карточка удалённого открывается (ссылка из уведомления не ведёт в 404)
        self.assertEqual(self.mod_client.get(f'/api/moderation/ads/{deleted.pk}/').status_code, 200)

    def test_approve_to_initial_status(self):
        """Маршрут ads/<id>/approve/ не уходит в старую модерацию заказов (order_type='ads')"""
        active, on_hold = self.new_ad(), self.new_ad(initial_status='ON_HOLD')
        for ad, expected in ((active, AdStatus.ACTIVE), (on_hold, AdStatus.ON_HOLD)):
            response = self.mod(f'ads/{ad.pk}/approve/')
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data['status'], expected)
            self.assertEqual(self.refresh(ad).status, expected)

    def test_decline_and_block_exact_refund(self):
        Balance.objects.filter(user=self.owner).update(carry=Decimal('0.004'))
        declined = self.new_ad()  # бюджет 10000.004 — carry ушёл в бюджет
        before = Balance.objects.get(user=self.owner).amount

        response = self.mod(f'ads/{declined.pk}/decline/', {'reason': 'Не по правилам'})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['refund_amount'], '10000.004')
        self.assertEqual(response.data['refund_wallet_amount'], '10000.00')  # на баланс — до копеек
        balance = Balance.objects.get(user=self.owner)
        self.assertEqual((balance.amount, balance.carry), (before + Decimal('10000.00'), Decimal('0.004')))
        self.assertEqual(self.refresh(declined).moderation_reason, 'Не по правилам')

        blocked = self.approved_ad()  # отказ вернул 0.004 в carry — он ушёл в бюджет этого объявления
        self.assertEqual(blocked.budget, Decimal('10000.004'))
        Ad.objects.filter(pk=blocked.pk).update(spent=Decimal('1234.5678'))
        response = self.mod(f'ads/{blocked.pk}/block/', {'reason': 'Нарушение'})
        self.assertEqual(response.data['refund_amount'], '8765.4362')  # 10000.004 − 1234.5678
        self.assertEqual(response.data['refund_wallet_amount'], '8765.43')
        self.assertEqual(self.refresh(blocked).status, AdStatus.BLOCKED)
        self.assertEqual(Op.objects.filter(kind=Op.Kind.REFUND).count(), 2)

    @override_settings(**ADGRAM_SETTINGS)
    def test_adgram_owner_gets_whole_sums(self):
        # Владелец из AdGram: в кошелёк уходят только целые сумы, дробь копится в carry
        owner = create_user_with_balance(username='ag_cl1', adgram_user_id='cl1')
        owner_client = client_for(owner)
        ok = OperationResult(status='done', balance_after=Decimal('0'))
        with patch('apps.billing.adgram_client.debit', return_value=ok):
            declined, blocked = (self.new_ad(owner_client) for _ in range(2))
        response = self.mod(f'ads/{declined.pk}/decline/', {'reason': 'Нет'})
        self.assertEqual((response.data['refund_amount'], response.data['refund_wallet_amount']),
                         ('10000.00', '10000.00'))

        self.assertEqual(self.mod(f'ads/{blocked.pk}/approve/').status_code, 200)
        Ad.objects.filter(pk=blocked.pk).update(spent=Decimal('0.4'))
        response = self.mod(f'ads/{blocked.pk}/block/', {'reason': 'Нарушение'})
        self.assertEqual((response.data['refund_amount'], response.data['refund_wallet_amount']),
                         ('9999.60', '9999.00'))
        self.assertEqual(Balance.objects.get(user=owner).carry, Decimal('0.6'))

    def test_reason_required(self):
        ad = self.new_ad()
        for path in (f'ads/{ad.pk}/decline/', f'ads/{ad.pk}/block/'):
            for data in ({}, {'reason': '   '}, {'reason': 'x' * 501}):
                with self.subTest(path=path, data=data):
                    response = self.mod(path, data)
                    self.assertEqual(response.status_code, 400)
                    self.assertIn('reason', response.data)
        self.assertEqual(self.refresh(ad).status, AdStatus.IN_REVIEW)

    def test_conflicts_409(self):
        ad = self.new_ad()
        self.mod(f'ads/{ad.pk}/approve/')
        self.assertEqual(self.mod(f'ads/{ad.pk}/approve/').status_code, 409)
        self.assertEqual(self.mod(f'ads/{ad.pk}/decline/', {'reason': 'поздно'}).status_code, 409)

    def test_moderator_moderates_own_ad(self):
        # Свои объявления модератор модерирует сам (у старых заказов и каналов запрет остаётся)
        own = self.new_ad(client=self.mod_client)
        self.assertEqual(self.mod(f'ads/{own.pk}/approve/').status_code, 200)
        self.assertEqual(self.refresh(own).status, AdStatus.ACTIVE)

        # Правка креатива модератором — не сразу, а через ревизию; её он тоже решает сам
        for action, data in (('approve', {}), ('decline', {'reason': 'Передумал'})):
            with self.subTest(action=action):
                response = self.mod_client.patch(f'/api/ads/{own.pk}/', {'text': f'Правка {action}'}, format='json')
                self.assertEqual(response.status_code, 202, response.data)
                revision = own.revisions.get(status=Rev.PENDING)
                self.assertEqual(self.mod(f'ads/revisions/{revision.pk}/{action}/', data).status_code, 200)
        self.assertEqual(self.refresh(own).text, 'Правка approve')

        declined = self.new_ad(client=self.mod_client)
        self.assertEqual(self.mod(f'ads/{declined.pk}/decline/', {'reason': 'Сам отклонил'}).status_code, 200)
        self.assertEqual(self.mod(f'ads/{own.pk}/block/', {'reason': 'Сам заблокировал'}).status_code, 200)
        self.assertEqual((self.refresh(declined).status, self.refresh(own).status),
                         (AdStatus.DECLINED, AdStatus.BLOCKED))

        decisions = AdDecision.objects.filter(ad__in=[own, declined])
        self.assertEqual(sorted(decisions.values_list('action', flat=True)),
                         sorted(['APPROVED', 'REVISION_APPROVED', 'REVISION_DECLINED', 'BLOCKED', 'DECLINED']))
        self.assertEqual(set(decisions.values_list('moderator', flat=True)), {self.moderator.pk})

    def test_advertiser_has_no_access(self):
        ad = self.new_ad()
        for method, path in (('get', 'ads/pending/'), ('get', 'ads/all/'), ('get', f'ads/{ad.pk}/'),
                             ('post', f'ads/{ad.pk}/approve/'), ('get', 'ads/revisions/pending/')):
            with self.subTest(path=path):
                response = getattr(self.owner_client, method)(f'/api/moderation/{path}')
                self.assertEqual(response.status_code, 403)

    def test_superadmin_ad_and_edits_approved_at_once(self):
        boss_client = client_for(self.boss)
        ad = self.new_ad(client=boss_client, initial_status='ON_HOLD')
        self.assertEqual(ad.status, AdStatus.ON_HOLD)  # сразу initial_status, без очереди
        self.assertEqual(ad.decisions.get().action, AdDecision.Action.APPROVED)
        self.assertFalse(Notification.objects.filter(type=Notification.Type.NEW_ORDER).exists())

        response = boss_client.patch(f'/api/ads/{ad.pk}/', {'text': 'Правка суперадмина'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.refresh(ad).text, 'Правка суперадмина')
        self.assertFalse(ad.revisions.exists())

    def test_pending_count(self):
        ad = self.approved_ad()
        self.new_ad()
        self.patch_ad(ad, {'text': 'Новый текст'})
        data = self.mod_client.get('/api/moderation/pending_count/').data
        self.assertEqual((data['total'], data['ads'], data['ad_revisions'], data['total_all']), (0, 1, 1, 2))

    def test_notifications(self):
        def last(user, notif_type):
            return Notification.objects.filter(user=user, type=notif_type).latest('created_at')

        ad = self.new_ad()
        note = last(self.moderator, Notification.Type.NEW_ORDER)
        self.assertEqual(note.payload, {'order_id': str(ad.pk), 'order_type': 'ads'})
        self.assertTrue(Notification.objects.filter(user=self.boss, type=Notification.Type.NEW_ORDER).exists())

        self.mod(f'ads/{ad.pk}/approve/')
        self.assertEqual(last(self.owner, Notification.Type.ORDER_APPROVED).payload['order_type'], 'ads')

        self.patch_ad(ad, {'text': 'Правка'})
        revision = ad.revisions.get()
        note = last(self.moderator, Notification.Type.NEW_REVISION)
        self.assertEqual(note.payload, {'order_id': str(ad.pk), 'order_type': 'ads', 'revision_id': str(revision.pk)})

        self.mod(f'ads/revisions/{revision.pk}/decline/', {'reason': 'Кликбейт'})
        self.assertIn('Кликбейт', last(self.owner, Notification.Type.REVISION_DECLINED).body)
        self.patch_ad(ad, {'text': 'Правка 2'})
        self.mod(f'ads/revisions/{ad.pending_revision().pk}/approve/')
        self.assertTrue(Notification.objects.filter(user=self.owner, type=Notification.Type.REVISION_APPROVED).exists())

        self.mod(f'ads/{ad.pk}/block/', {'reason': 'Жалобы'})
        self.assertIn('Жалобы', last(self.owner, Notification.Type.ORDER_BLOCKED).body)
        declined = self.new_ad()
        self.mod(f'ads/{declined.pk}/decline/', {'reason': 'Мошенничество'})
        self.assertIn('Мошенничество', last(self.owner, Notification.Type.ORDER_REJECTED).body)


class AdRevisionTests(ModerationBase):
    """Правки креатива: сразу (на модерации) или через ревизию (после одобрения)"""

    def test_in_review_edit_applied_at_once(self):
        ad = self.new_ad()
        response = self.patch_ad(ad, {'text': 'Исправил опечатку', 'other_info': 'erid: 123'})
        self.assertEqual(response.status_code, 200)
        ad = self.refresh(ad)
        self.assertEqual((ad.text, ad.other_info), ('Исправил опечатку', 'erid: 123'))
        self.assertFalse(ad.revisions.exists())

    def test_approved_edit_goes_to_revision(self):
        for status_name in ('ACTIVE', 'ON_HOLD', 'STOPPED'):
            with self.subTest(status=status_name):
                ad = self.approved_ad()
                Ad.objects.filter(pk=ad.pk).update(status=status_name)
                response = self.patch_ad(ad, {'text': 'Новый текст'})
                self.assertEqual(response.status_code, 202, response.data)
                self.assertEqual(response.data['pending_revision']['text'], 'Новый текст')
                self.assertEqual(response.data['text'], 'Свежие новости Узбекистана')  # крутится прежний
                self.assertEqual(self.refresh(ad).text, 'Свежие новости Узбекистана')

        revisions = self.mod_client.get('/api/moderation/ads/revisions/pending/').data['results']
        self.assertEqual(len(revisions), 3)
        self.assertEqual(revisions[0]['current']['text'], 'Свежие новости Узбекистана')
        self.assertEqual(revisions[0]['proposed']['text'], 'Новый текст')

    def test_new_edit_supersedes_and_builds_on_pending(self):
        ad = self.approved_ad()
        self.patch_ad(ad, {'text': 'Текст А'})
        first = ad.revisions.get()
        self.patch_ad(ad, {'other_info': 'erid: 1'})  # частичная правка — поверх висящей

        self.assertEqual(self.refresh(first).status, Rev.SUPERSEDED)
        second = ad.revisions.get(status=Rev.PENDING)
        self.assertEqual((second.text, second.other_info), ('Текст А', 'erid: 1'))
        self.assertEqual(self.mod(f'ads/revisions/{first.pk}/approve/').status_code, 409)

    def test_approve_revision(self):
        ad = self.approved_ad(initial_status='ON_HOLD')
        self.patch_ad(ad, {'text': 'Новый текст', 'other_info': 'erid: 7'})
        revision = ad.revisions.get()

        response = self.mod(f'ads/revisions/{revision.pk}/approve/')
        self.assertEqual(response.status_code, 200, response.data)
        ad = self.refresh(ad)
        self.assertEqual((ad.text, ad.other_info, ad.status), ('Новый текст', 'erid: 7', AdStatus.ON_HOLD))
        self.assertEqual(self.refresh(revision).status, Rev.APPROVED)
        self.assertEqual(ad.decisions.first().action, AdDecision.Action.REVISION_APPROVED)
        self.assertIsNone(self.owner_client.get(f'/api/ads/{ad.pk}/').data['pending_revision'])
        self.assertEqual(Op.objects.filter(order_id=ad.pk).count(), 1)  # ревизия деньги не двигает

    def test_decline_revision(self):
        ad = self.approved_ad()
        self.patch_ad(ad, {'text': 'Кликбейт!!!'})
        revision = ad.revisions.get()

        self.assertEqual(self.mod(f'ads/revisions/{revision.pk}/decline/', {}).status_code, 400)  # нужна причина
        self.assertEqual(self.mod(f'ads/revisions/{revision.pk}/decline/', {'reason': 'Кликбейт'}).status_code, 200)
        self.assertEqual(self.refresh(ad).text, 'Свежие новости Узбекистана')

        data = self.owner_client.get(f'/api/ads/{ad.pk}/').data
        self.assertIsNone(data['pending_revision'])
        self.assertEqual(data['last_declined_revision']['reason'], 'Кликбейт')
        self.patch_ad(ad, {'text': 'Спокойный текст'})  # новая правка — старый отказ больше не висит
        self.assertIsNone(self.owner_client.get(f'/api/ads/{ad.pk}/').data['last_declined_revision'])

    def test_block_and_delete_supersede_revision(self):
        blocked, deleted = self.approved_ad(), self.approved_ad()
        for ad in (blocked, deleted):
            self.patch_ad(ad, {'text': 'Правка'})
        self.mod(f'ads/{blocked.pk}/block/', {'reason': 'Нарушение'})
        self.owner_client.delete(f'/api/ads/{deleted.pk}/')
        for ad in (blocked, deleted):
            self.assertEqual(ad.revisions.get().status, Rev.SUPERSEDED)
        self.assertEqual(self.mod_client.get('/api/moderation/ads/revisions/pending/').data['count'], 0)

    def test_title_now_url_never(self):
        ad = self.approved_ad()
        response = self.patch_ad(ad, {'title': 'Осенняя кампания'})
        self.assertEqual(response.status_code, 200)  # title — внутреннее, без модерации
        self.assertEqual(self.refresh(ad).title, 'Осенняя кампания')
        self.assertFalse(ad.revisions.exists())

        response = self.patch_ad(ad, {'url': 't.me/daryo'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(str(response.data['url']), 'Ссылка после создания не меняется')

    def test_creative_edit_forbidden_for_declined_and_blocked(self):
        declined = self.new_ad()
        self.mod(f'ads/{declined.pk}/decline/', {'reason': 'нет'})
        blocked = self.approved_ad()
        self.mod(f'ads/{blocked.pk}/block/', {'reason': 'нет'})
        for ad in (declined, blocked):
            self.assertEqual(self.patch_ad(ad, {'text': 'Попробую ещё'}).status_code, 400)
        self.assertFalse(AdRevision.objects.exists())

    def test_creative_validation(self):
        ad = self.approved_ad()
        self.assertIn('text', self.patch_ad(ad, {'text': 'строка\nвторая'}).data)
        self.assertIn('text', self.patch_ad(ad, {'text': 'x' * 161}).data)
        search = self.approved_ad(placement=Placement.SEARCH)
        self.assertIn('text', self.patch_ad(search, {'text': 'текст'}).data)
        self.assertIn('show_channel_picture', self.patch_ad(search, {'show_channel_picture': True}).data)

        # Правка одного креатива без cpm — без проверки минимума: CPM пересчитается при одобрении
        self.assertEqual(self.patch_ad(ad, {'show_channel_picture': True}).status_code, 202)
        # cpm прислан явно — проверка как раньше: с фото канала минимум 1000 × 1.3 = 1300
        response = self.patch_ad(ad, {'show_channel_picture': True, 'cpm': 1200})
        self.assertEqual(response.data['cpm'][0], "CPM can't be less than 1300")
        self.assertEqual(self.patch_ad(ad, {'show_channel_picture': True, 'cpm': 1300}).status_code, 202)
        # Пока правка с фото на проверке, опустить CPM ниже минимума с фото нельзя
        self.assertEqual(self.patch_ad(ad, {'cpm': 1000}).data['cpm'][0], "CPM can't be less than 1300")

    def test_db_allows_only_one_pending_revision(self):
        ad = self.approved_ad()
        AdRevision.objects.create(ad=ad, text='A')
        with self.assertRaises(IntegrityError), transaction.atomic():
            AdRevision.objects.create(ad=ad, text='B')


class RevisionRaceTests(TransactionTestCase):
    """Гонка: модератор одобряет правку, а владелец в тот же момент присылает новую"""

    @patch('apps.notifications.services._enqueue_push')
    def test_approve_revision_vs_new_edit(self, _push):
        owner, moderator = make_local_user('owner'), make_moderator()
        response = create_ad(client_for(owner))
        ad = Ad.objects.get(pk=response.data['id'])
        with transaction.atomic():
            Ad.objects.select_for_update().get(pk=ad.pk).approve(moderator)
            first = Ad.objects.select_for_update().get(pk=ad.pk).update_creative({'text': 'Правка 1'}, owner)

        barrier = threading.Barrier(2)
        outcome = {}

        def approve():
            try:
                barrier.wait()
                with transaction.atomic():
                    locked_ad, revision = AdRevision.lock_with_ad(first.pk)
                    revision.approve(locked_ad, moderator)
                outcome['approve'] = 'ok'
            except AdStateError:
                outcome['approve'] = 'conflict'
            finally:
                connection.close()

        def new_edit():
            try:
                barrier.wait()
                with transaction.atomic():
                    Ad.objects.select_for_update().get(pk=ad.pk).update_creative({'text': 'Правка 2'}, owner)
            finally:
                connection.close()

        threads = [threading.Thread(target=approve), threading.Thread(target=new_edit)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        first = AdRevision.objects.get(pk=first.pk)
        ad = Ad.objects.get(pk=ad.pk)
        pending = AdRevision.objects.filter(ad=ad, status=Rev.PENDING)
        self.assertEqual(pending.count(), 1)  # новая правка всегда ждёт модератора, ровно одна
        self.assertEqual(pending.get().text, 'Правка 2')
        if outcome['approve'] == 'ok':
            # Одобрение успело первым: в объявлении правка 1, правка 2 на проверке
            self.assertEqual((first.status, ad.text), (Rev.APPROVED, 'Правка 1'))
        else:
            # Новая правка успела первой: правка 1 заменена, одобрять уже нечего, объявление прежнее
            self.assertEqual((first.status, ad.text), (Rev.SUPERSEDED, 'Свежие новости Узбекистана'))


class DisplayNameTests(ModerationBase):
    """Фронту — имя из AdGram вместо ag_<id>: display_name / *_name рядом со старыми полями"""

    def setUp(self):
        super().setUp()
        # Имя в AdGram ставим напрямую: кошелёк у владельца остаётся локальным
        for user, name in ((self.owner, 'Ali Valiyev'), (self.moderator, 'Moder AG')):
            user.adgram_username = name
            user.save(update_fields=['adgram_username'])
        self.boss_client = client_for(self.boss)

    def test_ad_owner_and_decision_moderator(self):
        ad = self.approved_ad()
        data = self.mod_client.get(f'/api/moderation/ads/{ad.pk}/').data
        self.assertEqual((data['owner']['username'], data['owner']['display_name']), ('owner', 'Ali Valiyev'))
        decision = data['decisions'][0]
        self.assertEqual((decision['action'], decision['moderator'], decision['moderator_name']),
                         (AdDecision.Action.APPROVED, 'moder', 'Moder AG'))
        pending = self.mod_client.get('/api/moderation/ads/all/').data['results'][0]
        self.assertEqual(pending['owner']['display_name'], 'Ali Valiyev')

    def test_reports_status_filter_and_reviewer_name(self):
        ad = self.approved_ad()
        spam = AdReport.objects.create(ad=ad, viewer_key='1' * 32, reason=AdReport.Reason.SPAM)
        AdReport.objects.create(ad=ad, viewer_key='2' * 32, reason=AdReport.Reason.OTHER)
        self.assertEqual(self.mod(f'ads/reports/{spam.pk}/review/').status_code, 200)

        def reports(query=''):
            groups = self.mod_client.get(f'/api/moderation/ads/reports/{query}').data['results']
            return sorted((report['reason'], report['status'], report['reviewed_by'], report['reviewed_by_name'])
                          for group in groups for report in group['reports'])

        self.assertEqual(reports('?status=NEW'), [('other', 'NEW', None, None)])
        self.assertEqual(reports('?status=REVIEWED'), [('spam', 'REVIEWED', 'moder', 'Moder AG')])
        self.assertEqual(len(reports()), 2)  # без параметра — все, как раньше
        self.assertEqual(len(reports('?status=WRONG')), 2)  # неизвестный статус не фильтрует

    def test_channels_owner_and_editor_names(self):
        ChannelEarning.objects.create(channel_id=-1001000000009, channel_name='Kanal', owner=self.owner,
                                      claim_status=ChannelEarning.ClaimStatus.CONFIRMED)
        ChannelEarning.objects.create(channel_id=-1001000000010, channel_name='Ничей')
        response = self.mod_client.patch('/api/moderation/channels/-1001000000009/',
                                         {'language': 'uz', 'topics': [SPORTS]}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data['meta_updated_by'], response.data['meta_updated_by_name']),
                         ('moder', 'Moder AG'))

        results = self.mod_client.get('/api/moderation/channels/').data['results']
        channels = {item['channel_id']: item for item in results}
        self.assertEqual((channels[-1001000000009]['owner'], channels[-1001000000009]['owner_name']),
                         ('owner', 'Ali Valiyev'))
        self.assertIsNone(channels[-1001000000010]['owner_name'])

        earnings = self.boss_client.get('/api/partner/admin/earnings/').data['results']
        self.assertEqual({item['channel_id']: item['owner_name'] for item in earnings},
                         {-1001000000009: 'Ali Valiyev', -1001000000010: None})

    def test_profile_balance_users_and_banners(self):
        profile = self.owner_client.get('/api/auth/profile/').data
        self.assertEqual((profile['username'], profile['display_name']), ('owner', 'Ali Valiyev'))
        # Без имени в AdGram — обычный username
        self.assertEqual(self.boss_client.get('/api/auth/profile/').data['display_name'], 'boss')
        self.assertEqual(self.owner_client.get('/api/balance/').data['display_name'], 'Ali Valiyev')

        users = {item['username']: item['display_name']
                 for item in self.boss_client.get('/api/auth/users/?page_size=100').data['results']}
        self.assertEqual(users, {'boss': 'boss', 'moder': 'Moder AG', 'owner': 'Ali Valiyev'})

        response = self.boss_client.post('/api/banner_ads/manage/', {'title': 'Баннер', 'text': 'Текст'},
                                          format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual((response.data['created_by'], response.data['created_by_name']), ('boss', 'boss'))


class LabelStyleModerationTests(ModerationBase):
    """PATCH /api/moderation/ads/<id>/: модератор / суперадмин меняет подпись любого объявления сразу, без модерации"""

    def moderate_label(self, ad, data, client=None):
        return (client or self.mod_client).patch(f'/api/moderation/ads/{ad.pk}/', data, format='json')

    def test_moderator_and_superadmin_change_active_ad(self):
        ad = self.approved_ad()
        response = self.moderate_label(ad, {'label_style': 'PLAIN'})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data['label_style'], response.data['status'], response.data['pending_revision']),
                         (LabelStyle.PLAIN, AdStatus.ACTIVE, None))
        ad = self.refresh(ad)
        self.assertEqual((ad.label_style, ad.status), (LabelStyle.PLAIN, AdStatus.ACTIVE))
        self.assertFalse(ad.revisions.exists())

        response = self.moderate_label(ad, {'label_style': 'NOVAGRAM'}, client=client_for(self.boss))
        self.assertEqual((response.status_code, self.refresh(ad).label_style), (200, LabelStyle.NOVAGRAM))

    def test_owner_is_forbidden(self):
        ad = self.approved_ad()
        self.assertEqual(self.moderate_label(ad, {'label_style': 'PLAIN'}, client=self.owner_client).status_code, 403)
        self.assertEqual(self.refresh(ad).label_style, LabelStyle.NOVAGRAM)

    def test_only_label_style_and_valid_value(self):
        ad = self.approved_ad()
        response = self.moderate_label(ad, {'label_style': 'PLAIN', 'cpm': 1})
        self.assertEqual(response.status_code, 400)
        self.assertIn('cpm', response.data)
        self.assertEqual(self.moderate_label(ad, {'label_style': 'Ad'}).status_code, 400)
        self.assertEqual(self.moderate_label(ad, {}).status_code, 400)
        self.assertEqual(self.refresh(ad).label_style, LabelStyle.NOVAGRAM)

    def test_deleted_ad(self):
        ad = self.approved_ad()
        Ad.objects.filter(pk=ad.pk).update(status=AdStatus.DELETED)
        self.assertEqual(self.moderate_label(ad, {'label_style': 'PLAIN'}).status_code, 409)
