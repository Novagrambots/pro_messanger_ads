"""
Тесты этапа 8: события action и video_open, итоги на объявлении, список (поиск, сортировка по
каждому столбцу, формулы, сводка таргетинга), настройки столбцов, статистика по дням.
"""
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.ads.models import (DEFAULT_TABLE_COLUMNS, TABLE_COLUMNS, Ad, AdDailyStat, AdMedia, AdRevision, AdStatus,
                             Placement)
from apps.ads.serving import events, snapshot, tokens
from apps.ads.test_serving import ServingBase
from apps.ads.tests import (client_for, create_ad, ensure_test_peers, make_local_user, setUpModule,  # noqa: F401
                            tearDownModule)
from apps.directories.models import Location, TelegramPeer

TASHKENT = ZoneInfo('Asia/Tashkent')


class ActionAndVideoEventsTests(ServingBase):
    """action — один раз на токен и только после клика; video_open — один раз, только у видео; оба бесплатно"""

    def setUp(self):
        super().setUp()
        self.ad = self.active_ad(views_per_user_per_day=0)
        self.reload()

    def test_action_after_click_once_and_free(self):
        token = self.token(self.ad)
        action = {'random_id': token, 'type': 'action'}
        self.assertEqual(self.sent(1, action), (0, 1))  # клика ещё не было
        self.assertEqual(self.sent(1, self.click(token)), (1, 0))
        self.assertEqual(self.sent(1, action, action), (1, 1))  # второй раз по тому же токену — нет
        other = self.token(self.ad, 2)
        self.assertEqual(self.sent(2, {'random_id': other, 'type': 'action'}), (0, 1))  # клик был по другому токену
        # Клик отклонён окном 5 с (тот же зритель) — переход не засчитан, действие тоже
        again = self.token(self.ad)
        self.assertEqual(self.sent(1, self.click(again)), (0, 1))
        self.assertEqual(self.sent(1, {'random_id': again, 'type': 'action'}), (0, 1))
        self.assertEqual(self.budget_left(self.ad), 10_000_000)  # бесплатно
        self.flush()
        stat = self.stat(self.ad)
        self.assertEqual((stat.clicks, stat.actions), (1, 1))
        self.assertEqual((self.fresh(self.ad).clicks_total, self.fresh(self.ad).actions_total), (1, 1))

    def test_video_open_once_and_only_for_video(self):
        token = self.token(self.ad)
        opened = {'random_id': token, 'type': 'video_open'}
        self.assertEqual(self.sent(1, opened), (0, 1))  # у объявления нет видео
        media = AdMedia.objects.create(user=self.owner, file='ads/media/v.mp4', media_type=AdMedia.MediaType.VIDEO,
                                       width=1280, height=720, status=AdMedia.Status.READY)
        Ad.objects.filter(pk=self.ad.pk).update(media=media)
        self.reload()
        self.assertEqual(self.sent(1, opened, opened), (1, 1))  # без клика можно, но один раз
        self.assertEqual(self.budget_left(self.ad), 10_000_000)
        self.flush()
        self.assertEqual(self.stat(self.ad).video_opens, 1)
        self.assertEqual(self.fresh(self.ad).video_opens_total, 1)

    def test_totals_match_daily_stats(self):
        snap = snapshot.current()
        days = [datetime(2026, 10, 15, 12, tzinfo=TASHKENT), datetime(2026, 10, 16, 12, tzinfo=TASHKENT)]
        for day_index, moment in enumerate(days):
            for user_id in range(1, 4 + day_index):
                viewer = tokens.viewer_key(user_id)
                token = tokens.issue(str(self.ad.pk), viewer, Placement.SEARCH, b'\0' * 8,
                                     now=moment.timestamp())
                items = [self.view(token), self.click(token), {'random_id': token, 'type': 'action'}]
                events.process(snap, viewer, items, now=moment)
            self.flush()
        Ad.objects.filter(pk=self.ad.pk).update(status=AdStatus.ON_HOLD)  # и бесплатный показ
        self.reload()
        viewer = tokens.viewer_key(99)
        events.process(snapshot.current(), viewer, [self.view(tokens.issue(str(self.ad.pk), viewer, Placement.SEARCH,
                                                                           b'\0' * 8))])
        self.flush()

        ad = self.fresh(self.ad)
        rows = AdDailyStat.objects.filter(ad=ad)
        self.assertEqual(rows.count(), 3)  # два дня + сегодня
        sums = {field: sum(getattr(row, field) for row in rows)
                for field in ('views', 'free_views', 'clicks', 'actions', 'video_opens', 'spent')}
        self.assertEqual((ad.views_total, ad.free_views_total, ad.clicks_total, ad.actions_total, ad.video_opens_total,
                          ad.spent),
                         (sums['views'], sums['free_views'], sums['clicks'], sums['actions'], sums['video_opens'],
                          sums['spent']))
        # Клики второго дня от тех же зрителей пришли через миг — окно 5 с их не засчитало (и действия тоже)
        self.assertEqual((ad.views_total, ad.free_views_total, ad.clicks_total, ad.actions_total), (7, 1, 4, 4))


class AdListTests(TestCase):
    """GET /api/ads/: поиск, сортировка по каждому столбцу, формулы, сводка таргетинга, тип действия"""

    @classmethod
    def setUpTestData(cls):
        cls.user = make_local_user('owner')

    def setUp(self):
        self.client = client_for(self.user)

    def new_ad(self, placement=Placement.SEARCH, **overrides):
        response = create_ad(self.client, placement, **overrides)
        self.assertEqual(response.status_code, 201, response.data)
        return Ad.objects.get(pk=response.data['id'])

    def listed(self, query=''):
        response = self.client.get(f'/api/ads/?page_size=100{query}')
        self.assertEqual(response.status_code, 200, response.data)
        return response.data['results']

    def ids(self, query=''):
        return [item['id'] for item in self.listed(query)]

    def test_search_by_title_link_and_username(self):
        kunuz = self.new_ad(title='Kun.uz yangiliklar')
        daryo = self.new_ad(title='Sport', url='https://t.me/daryo')
        self.assertEqual(self.ids('&q=KUN.UZ'), [str(kunuz.pk)])         # название, регистр не важен
        self.assertEqual(self.ids('&q=t.me/daryo'), [str(daryo.pk)])     # ссылка
        self.assertEqual(self.ids('&q=@daryo'), [str(daryo.pk)])         # username продвигаемого
        self.assertEqual(set(self.ids('&q=')), {str(kunuz.pk), str(daryo.pk)})
        self.assertEqual(self.ids('&q=nothing'), [])

    def make_three(self):
        """Три объявления с разными значениями во всех столбцах"""
        a = self.new_ad(title='alpha', cpm=1000, budget='10000')
        b = self.new_ad(title='Bravo', cpm=3000, budget='20000', url='https://t.me/daryo')
        c = self.new_ad(Placement.BOT, title='charlie', cpm=2000, budget='15000', url='https://t.me/weatherbot')
        now = timezone.now()
        values = {
            a: dict(views_total=100, free_views_total=0, clicks_total=0, actions_total=0, video_opens_total=5,
                    spent=Decimal('100'), status=AdStatus.ACTIVE, created_at=now - timedelta(days=3)),
            b: dict(views_total=50, free_views_total=50, clicks_total=10, actions_total=5, video_opens_total=1,
                    spent=Decimal('300'), status=AdStatus.ON_HOLD, created_at=now - timedelta(days=2)),
            c: dict(views_total=0, free_views_total=0, clicks_total=4, actions_total=1, video_opens_total=0,
                    spent=Decimal('8'), status=AdStatus.STOPPED, created_at=now - timedelta(days=1)),
        }
        for ad, fields in values.items():
            Ad.objects.filter(pk=ad.pk).update(**fields)
        return str(a.pk), str(b.pk), str(c.pk)

    def test_ordering_by_each_column(self):
        a, b, c = self.make_three()
        # remaining: a 9900, b 19700, c 14992; views: a 100, b 100 (50+50), c 0
        expected = {
            'created_at': [a, b, c], 'title': [a, b, c], 'url': [b, a, c], 'status': [a, b, c], 'cpm': [a, c, b],
            'spent': [c, a, b], 'remaining_budget': [a, c, b], 'opened_video': [c, b, a], 'clicks': [a, c, b],
            'actions': [a, c, b],
        }
        for column, ascending in expected.items():
            with self.subTest(column=column):
                self.assertEqual(self.ids(f'&ordering={column}'), ascending)
                self.assertEqual(self.ids(f'&ordering=-{column}'), ascending[::-1])
        # Тип действия: у a и b одинаковый JOIN — между ними порядок по дате, новые выше
        self.assertEqual(self.ids('&ordering=action_type'), [b, a, c])
        self.assertEqual(self.ids('&ordering=-action_type'), [c, b, a])
        self.assertEqual(self.ids('&ordering=views')[0], c)
        self.assertEqual(self.ids('&ordering=-views')[-1], c)
        self.assertEqual(self.ids(), [c, b, a])  # по умолчанию — новые сверху

    def test_ordering_computed_with_nulls_last(self):
        a, b, c = self.make_three()
        # ctr: a 0/100=0, b 10/100=0.1, c 4/0=null; cvr: a null, b 0.5, c 0.25
        # cpc: a null, b 30, c 2; cpa: a null, b 60, c 8
        cases = {'ctr': ([a, b, c], [b, a, c]), 'cvr': ([c, b, a], [b, c, a]),
                 'cpc': ([c, b, a], [b, c, a]), 'cpa': ([c, b, a], [b, c, a])}
        for column, (ascending, descending) in cases.items():
            with self.subTest(column=column):
                self.assertEqual(self.ids(f'&ordering={column}'), ascending)
                self.assertEqual(self.ids(f'&ordering=-{column}'), descending)  # null — в конце и так и так

    def test_bad_ordering(self):
        for ordering in ('targeting', '-targeting', 'foo', 'budget'):
            response = self.client.get(f'/api/ads/?ordering={ordering}')
            self.assertEqual(response.status_code, 400, ordering)
            self.assertIn('ordering', response.data)

    def test_formulas_and_division_by_zero(self):
        ad = self.new_ad()
        Ad.objects.filter(pk=ad.pk).update(views_total=600, free_views_total=100, clicks_total=2, actions_total=1,
                                           video_opens_total=3, spent=Decimal('0.4'))
        item = self.listed()[0]
        self.assertEqual((item['views'], item['opened_video'], item['clicks'], item['actions']), (700, 3, 2, 1))
        self.assertEqual((item['ctr'], item['cvr'], item['cpc'], item['cpa']), (0.002857, 0.5, '0.20', '0.40'))
        self.assertEqual((item['spent'], item['cpm'], item['status']), ('0.40', 1000, AdStatus.IN_REVIEW))
        Ad.objects.filter(pk=ad.pk).update(views_total=0, free_views_total=0, clicks_total=0, actions_total=0)
        item = self.listed()[0]
        self.assertEqual((item['ctr'], item['cvr'], item['cpc'], item['cpa']), (None, None, None, None))
        Ad.objects.filter(pk=ad.pk).update(clicks_total=3, spent=Decimal('1'))
        self.assertEqual(self.listed()[0]['cpc'], '0.333333')  # дробное — до 6 знаков

    def test_targeting_summary_for_each_placement(self):
        ensure_test_peers()
        TelegramPeer.objects.create(username='newsbot', peer_type=TelegramPeer.PeerType.BOT, title='newsbot',
                                    fetched_at=timezone.now())
        tashkent = Location.objects.get(name_en='Tashkent').pk
        cases = [
            (Placement.SEARCH, {'queries': ['новости uz', 'sport news', 'ob-havo']}, {'kind': 'queries', 'count': 3}),
            (Placement.BOT, {'bots': ['weatherbot', 'newsbot']}, {'kind': 'bots', 'count': 2}),
            (Placement.BOT, {'bots': []}, {'kind': 'all_bots', 'count': None}),
            (Placement.CHANNEL_MESSAGE, {'channels': ['daryo']}, {'kind': 'channels', 'count': 1}),
            (Placement.CHANNEL_MESSAGE, {'languages': ['uz', 'ru']}, {'kind': 'languages', 'count': 2}),
            (Placement.USERS_CHANNEL_MESSAGE, {'countries': ['UZ', 'KZ', 'TJ']}, {'kind': 'countries', 'count': 3}),
            (Placement.USERS_VIDEO_BANNER, {'countries': ['UZ'], 'locations': [tashkent]},
             {'kind': 'locations', 'count': 1}),
            (Placement.USERS_BOT_BANNER, {'countries': ['UZ']}, {'kind': 'countries', 'count': 1}),
        ]
        for placement, targeting, summary in cases:
            with self.subTest(placement=placement, targeting=targeting):
                ad = self.new_ad(placement, targeting=targeting)
                self.assertEqual(self.client.get(f'/api/ads/{ad.pk}/').data['targeting_summary'], summary)

    def test_action_type(self):
        channel = self.new_ad()
        bot = self.new_ad(Placement.BOT, url='https://t.me/weatherbot')
        legacy = self.new_ad()
        Ad.objects.filter(pk=legacy.pk).update(promoted_peer=None, url='https://t.me/oldschoolbot')
        types = {item['id']: item['action_type'] for item in self.listed()}
        self.assertEqual(types, {str(channel.pk): 'JOIN', str(bot.pk): 'START', str(legacy.pk): 'START'})

    def test_page_size_and_constant_queries(self):
        def list_queries():
            with CaptureQueriesContext(connection) as queries:
                self.listed()
            return len(queries)

        first = self.new_ad()
        AdRevision.objects.create(ad=first, text='')  # правка на проверке — подгружается заранее, не по строке
        few = list_queries()
        for _ in range(6):
            self.new_ad()
        self.assertEqual(list_queries(), few)  # число запросов не растёт с числом строк
        self.assertEqual(len(self.client.get('/api/ads/').data['results']), 5)  # по умолчанию 5
        self.assertEqual(len(self.client.get('/api/ads/?page_size=1000').data['results']), 7)  # максимум 100
        self.assertIsNotNone(next(item for item in self.listed() if item['id'] == str(first.pk))['pending_revision'])


class TableSettingsTests(TestCase):
    """Столбцы таблицы: по умолчанию, свои (порядок, без дублей), недопустимый → 400"""

    @classmethod
    def setUpTestData(cls):
        cls.user = make_local_user('owner')
        cls.other = make_local_user('other')

    def test_default_and_custom(self):
        client = client_for(self.user)
        data = client.get('/api/ads/table-settings/').data
        self.assertEqual(data['columns'], list(DEFAULT_TABLE_COLUMNS))
        self.assertEqual((data['available'], data['default']), (list(TABLE_COLUMNS), list(DEFAULT_TABLE_COLUMNS)))
        self.assertEqual(data['columns'],
                         ['views', 'clicks', 'actions', 'cpm', 'spent', 'remaining_budget', 'url', 'status',
                          'created_at'])

        response = client.put('/api/ads/table-settings/', {'columns': ['ctr', 'views', 'ctr', 'targeting']},
                              format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['columns'], ['ctr', 'views', 'targeting'])
        self.assertEqual(client.get('/api/ads/table-settings/').data['columns'], ['ctr', 'views', 'targeting'])
        self.assertEqual(client_for(self.other).get('/api/ads/table-settings/').data['columns'],
                         list(DEFAULT_TABLE_COLUMNS))  # у другого — своё
        self.assertEqual(client.put('/api/ads/table-settings/', {'columns': []}, format='json').data['columns'], [])

    def test_validation_and_auth(self):
        client = client_for(self.user)
        for body in ({'columns': ['views', 'budget']}, {'columns': 'views'}, {}):
            self.assertEqual(client.put('/api/ads/table-settings/', body, format='json').status_code, 400, body)
        from rest_framework.test import APIClient
        self.assertEqual(APIClient().get('/api/ads/table-settings/').status_code, 401)


class AdStatsTests(TestCase):
    """GET /api/ads/<id>/stats/: дни по Ташкенту, нули, итоги и метрики, границы периода, чужое → 404"""

    @classmethod
    def setUpTestData(cls):
        cls.user = make_local_user('owner')
        cls.other = make_local_user('other')

    def setUp(self):
        self.client = client_for(self.user)
        response = create_ad(self.client, Placement.SEARCH)
        self.ad = Ad.objects.get(pk=response.data['id'])

    def stats(self, query='', client=None):
        return (client or self.client).get(f'/api/ads/{self.ad.pk}/stats/{query}')

    def test_days_totals_and_metrics(self):
        day = datetime(2026, 10, 1).date()
        AdDailyStat.objects.create(ad=self.ad, date=day, views=90, free_views=10, clicks=4, actions=1, video_opens=2,
                                   spent=Decimal('0.09'))
        AdDailyStat.objects.create(ad=self.ad, date=day + timedelta(days=2), views=100, clicks=1,
                                   spent=Decimal('0.1'))
        AdDailyStat.objects.create(ad=self.ad, date=day + timedelta(days=5), views=999)  # вне периода
        response = self.stats('?from=2026-10-01&to=2026-10-03')
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data
        self.assertEqual([item['date'] for item in data['days']],
                         [day, day + timedelta(days=1), day + timedelta(days=2)])
        self.assertEqual(data['days'][1], {'date': day + timedelta(days=1), 'views': 0, 'free_views': 0, 'clicks': 0,
                                           'actions': 0, 'video_opens': 0, 'spent': '0.00'})
        self.assertEqual(data['days'][0]['spent'], '0.09')
        totals = data['totals']
        self.assertEqual((totals['views'], totals['free_views'], totals['clicks'], totals['actions'],
                          totals['video_opens'], totals['spent']), (190, 10, 5, 1, 2, '0.19'))
        self.assertEqual((totals['ctr'], totals['cvr'], totals['cpc'], totals['cpa']), (0.025, 0.2, '0.038', '0.19'))

    def test_default_period_and_bounds(self):
        today = timezone.localdate()
        data = self.stats().data
        self.assertEqual((len(data['days']), data['to'], data['from']), (30, today, today - timedelta(days=29)))
        self.assertEqual((data['totals']['ctr'], data['totals']['cpc']), (None, None))
        self.assertEqual(len(self.stats('?from=2026-01-01&to=2026-01-01').data['days']), 1)
        self.assertEqual(len(self.stats('?from=2025-01-01&to=2026-01-01').data['days']), 366)  # ровно год — можно
        for query in ('?from=2025-01-01&to=2026-01-02', '?from=2026-02-01&to=2026-01-01', '?from=01.10.2026',
                      '?to=2026-13-01'):
            response = self.stats(query)
            self.assertEqual(response.status_code, 400, query)
            self.assertIn('error', response.data)

    def test_foreign_and_deleted_ad(self):
        self.assertEqual(self.stats(client=client_for(self.other)).status_code, 404)
        self.client.delete(f'/api/ads/{self.ad.pk}/')
        self.assertEqual(self.stats().status_code, 404)
