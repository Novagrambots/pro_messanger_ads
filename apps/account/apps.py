from django.apps import AppConfig


class AccountConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.account'

    def ready(self):
        # При старте падаем с понятной ошибкой, если секрет AdGram совпал с SECRET_KEY
        from apps.account.authentication import check_adgram_secret
        check_adgram_secret()
