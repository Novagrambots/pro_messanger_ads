from django.apps import AppConfig


class DirectoriesConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.directories'
    verbose_name = 'Справочники и данные Telegram'

    def ready(self):
        # Правка справочника в админке сбрасывает их кэш
        from apps.directories import signals  # noqa: F401
