from django.apps import AppConfig


class AdsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.ads'
    verbose_name = 'Объявления (v2)'

    def ready(self):
        # Любое сохранение объявления → после коммита повышается версия снимка выдачи
        from django.db.models.signals import post_save

        from apps.ads.models import Ad
        from apps.ads.serving.snapshot import ad_saved
        post_save.connect(ad_saved, sender=Ad, dispatch_uid='ads_serving_snapshot_version')
