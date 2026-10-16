from django.contrib import admin

from apps.directories.models import Country, Language, Location, TelegramPeer, Topic


class SuperAdminOnly(admin.ModelAdmin):
    """Справочники правит только суперадмин. Удалять не даём — выключаем галкой is_active"""
    list_editable = ('is_active',)
    list_per_page = 100

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Language)
class LanguageAdmin(SuperAdminOnly):
    list_display = ('code', 'name_en', 'name_ru', 'name_uz', 'is_active')
    search_fields = ('code', 'name_en', 'name_ru', 'name_uz')
    list_filter = ('is_active',)


@admin.register(Topic)
class TopicAdmin(SuperAdminOnly):
    list_display = ('id', 'name_en', 'name_ru', 'name_uz', 'is_active')
    search_fields = ('name_en', 'name_ru', 'name_uz')
    list_filter = ('is_active',)


@admin.register(Country)
class CountryAdmin(SuperAdminOnly):
    list_display = ('code', 'name_en', 'name_ru', 'name_uz', 'is_active')
    search_fields = ('code', 'name_en', 'name_ru', 'name_uz')
    list_filter = ('is_active',)


@admin.register(Location)
class LocationAdmin(SuperAdminOnly):
    list_display = ('name_en', 'name_ru', 'name_uz', 'region_en', 'country', 'is_active')
    search_fields = ('name_en', 'name_ru', 'name_uz', 'region_en')
    list_filter = ('is_active', 'country')


@admin.register(TelegramPeer)
class TelegramPeerAdmin(admin.ModelAdmin):
    """Кэш данных Telegram — только просмотр (обновляется сам)"""
    list_display = ('username', 'peer_type', 'telegram_id', 'title', 'subscribers', 'not_found', 'fetched_at')
    list_filter = ('peer_type', 'not_found')
    search_fields = ('username', 'title')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser
