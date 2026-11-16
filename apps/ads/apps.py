from django.apps import AppConfig


class AdsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.ads'
    verbose_name = 'Объявления (v2)'

    def ready(self):
        # Любое сохранение объявления, канала партнёрки (язык, темы, заявка, доля) или цен →
        # после коммита повышается версия снимка выдачи
        from django.db.models.signals import post_save

        from apps.ads.models import Ad, AdPricing
        from apps.ads.serving.snapshot import ad_saved
        from apps.partner.models import ChannelEarning
        post_save.connect(ad_saved, sender=Ad, dispatch_uid='ads_serving_snapshot_version')
        post_save.connect(ad_saved, sender=ChannelEarning, dispatch_uid='ads_serving_snapshot_version_channels')
        # Тайминги баннера в видео живут в «Ценах и правилах»
        post_save.connect(ad_saved, sender=AdPricing, dispatch_uid='ads_serving_snapshot_version_pricing')
        # Мост старых версий: в снимке — индекс живых старых заказов (одобрили, отменили — перечитать сразу)
        from apps.ads.serving.legacy import order_saved
        from apps.chat_ads.models import ChatAdOrder
        from apps.search_ads.models import Order
        post_save.connect(order_saved, sender=Order, dispatch_uid='ads_serving_snapshot_version_legacy_search')
        post_save.connect(order_saved, sender=ChatAdOrder, dispatch_uid='ads_serving_snapshot_version_legacy_chat')
