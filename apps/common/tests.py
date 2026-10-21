from django.test import RequestFactory, SimpleTestCase, override_settings

from apps.common.permissions import has_server_api_key, server_api_keys


class ServerApiKeysTests(SimpleTestCase):
    """Несколько серверных ключей X-API-Key: смена ключа не отрезает старые версии приложения"""

    def check(self, key=None):
        headers = {} if key is None else {'HTTP_X_API_KEY': key}
        return has_server_api_key(RequestFactory().post('/', **headers))

    @override_settings(AD_SERVER_API_KEYS=' new-key , old-key ,', AD_SERVER_API_KEY='legacy-key')
    def test_any_of_keys(self):
        self.assertEqual(server_api_keys(), ['new-key', 'old-key', 'legacy-key'])
        for key in ('new-key', 'old-key', 'legacy-key'):
            self.assertTrue(self.check(key), key)
        for key in (None, '', 'wrong', 'new-key ', 'new-key,old-key'):
            self.assertFalse(self.check(key), key)

    @override_settings(AD_SERVER_API_KEYS='', AD_SERVER_API_KEY='legacy-key')
    def test_only_old_setting(self):
        # Как раньше: только AD_SERVER_API_KEY
        self.assertTrue(self.check('legacy-key'))
        self.assertFalse(self.check('wrong'))

    @override_settings(AD_SERVER_API_KEYS='new-key', AD_SERVER_API_KEY=None)
    def test_only_list(self):
        self.assertTrue(self.check('new-key'))

    @override_settings(AD_SERVER_API_KEYS=' , ', AD_SERVER_API_KEY=None)
    def test_no_keys_closed(self):
        # Ключей нет (пустые элементы не в счёт) — закрыто для всех, даже для пустого заголовка
        self.assertEqual(server_api_keys(), [])
        for key in (None, '', ' ', 'anything'):
            self.assertFalse(self.check(key), key)
