from django.contrib import admin, messages
from django.db import transaction

from apps.ads.models import Ad, AdDecision, AdMedia, AdPricing, AdRevision, AdStateError


class ReadOnlyInline(admin.TabularInline):
    """Вложенный список только для просмотра"""
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class AdRevisionInline(ReadOnlyInline):
    model = AdRevision
    fields = ('created_at', 'status', 'text', 'show_channel_picture', 'other_info', 'media', 'reason',
              'moderated_by')
    readonly_fields = fields


class AdDecisionInline(ReadOnlyInline):
    model = AdDecision
    fields = ('created_at', 'action', 'reason', 'moderator', 'revision')
    readonly_fields = fields


@admin.register(Ad)
class AdAdmin(admin.ModelAdmin):
    """
    Объявления v2 — только просмотр и действия модерации (approve / decline / block).
    Карточку не сохраняем вообще: полное сохранение без лока затёрло бы деньги, которые
    параллельно меняет выдача. Деньги и статусы — только методами модели под локом.
    Полноценный API модераторов (с причиной отказа) — этап 4.
    """
    list_display = ('title', 'user', 'placement', 'status', 'cpm', 'budget', 'spent', 'refunded', 'created_at')
    list_filter = ('status', 'placement')
    search_fields = ('title', 'url', 'user__username', 'user__adgram_username')
    list_select_related = ('user',)
    list_per_page = 30
    actions = ['approve_ads', 'decline_ads', 'block_ads']
    inlines = [AdRevisionInline, AdDecisionInline]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False  # удаление — мягкое, через API (с возвратом денег)

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return False  # только просмотр — см. докстринг

    def _apply(self, request, queryset, method_name, done_text, **kwargs):
        """Каждое объявление — в своей транзакции под локом; не тот статус — пропускаем"""
        done = skipped = 0
        for pk in queryset.values_list('pk', flat=True):
            try:
                with transaction.atomic():
                    ad = Ad.objects.select_for_update().get(pk=pk)
                    getattr(ad, method_name)(request.user, **kwargs)
                done += 1
            except AdStateError:
                skipped += 1
        self.message_user(request, f'{done_text}: {done}. Пропущено (не тот статус): {skipped}',
                          level=messages.WARNING if skipped else messages.SUCCESS)

    @admin.action(description='Одобрить', permissions=['view'])
    def approve_ads(self, request, queryset):
        self._apply(request, queryset, 'approve', 'Одобрено')

    @admin.action(description='Отклонить (весь остаток вернётся)', permissions=['view'])
    def decline_ads(self, request, queryset):
        self._apply(request, queryset, 'decline', 'Отклонено', reason='Отклонено через админку')

    @admin.action(description='Заблокировать (остаток вернётся)', permissions=['view'])
    def block_ads(self, request, queryset):
        self._apply(request, queryset, 'block', 'Заблокировано', reason='Заблокировано через админку')


@admin.register(AdPricing)
class AdPricingAdmin(admin.ModelAdmin):
    """Цены и правила — одна строка (создаётся миграцией), правит только суперадмин"""
    readonly_fields = ('updated_at',)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser


@admin.register(AdRevision)
class AdRevisionAdmin(admin.ModelAdmin):
    """Правки креатива — только просмотр (решения — через API модерации)"""
    list_display = ('ad', 'status', 'text', 'show_channel_picture', 'created_at', 'moderated_by')
    list_filter = ('status',)
    search_fields = ('ad__title', 'text', 'ad__user__username')
    list_select_related = ('ad', 'moderated_by')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser


@admin.register(AdMedia)
class AdMediaAdmin(admin.ModelAdmin):
    """Медиа объявлений — только просмотр (неприкреплённые старше суток удаляет beat)"""
    list_display = ('id', 'user', 'media_type', 'status', 'width', 'height', 'duration', 'size', 'created_at')
    list_filter = ('media_type', 'status')
    search_fields = ('id', 'user__username')
    list_select_related = ('user',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

