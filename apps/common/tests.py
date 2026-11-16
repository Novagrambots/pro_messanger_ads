import importlib
import os
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase, override_settings

from core import settings as project_settings

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


class CsrfTrustedOriginsExtraTests(SimpleTestCase):
    """CSRF_TRUSTED_ORIGINS_EXTRA из env дописывается к зашитому списку (стенд на другом домене)"""

    def origins(self, extra=None):
        env = {} if extra is None else {'CSRF_TRUSTED_ORIGINS_EXTRA': extra}
        # load_dotenv выключаем: иначе переменная из локального .env подмешалась бы в тест
        with patch.dict(os.environ, env), patch('dotenv.load_dotenv'):
            if extra is None:
                os.environ.pop('CSRF_TRUSTED_ORIGINS_EXTRA', None)
            return importlib.reload(project_settings).CSRF_TRUSTED_ORIGINS

    def tearDown(self):
        # Модуль настроек — как при старте (живые django.conf.settings перезагрузка не трогает)
        importlib.reload(project_settings)

    def test_extra_origins(self):
        base = self.origins()
        self.assertIn('https://ads-api.vipads.uz', base)
        self.assertEqual(self.origins(''), base)
        self.assertEqual(self.origins(' https://novadev.vipads.uz , ,https://b.test'),
                         [*base, 'https://novadev.vipads.uz', 'https://b.test'])
