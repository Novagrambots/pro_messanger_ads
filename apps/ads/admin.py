from django.contrib import admin, messages
from django.db import transaction

from apps.ads.models import Ad, AdPricing, AdStateError


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
