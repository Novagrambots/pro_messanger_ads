from django import forms
from django.contrib import admin, messages
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.billing.models import Balance, MoneyOperation
from apps.billing.services import log_admin_deposit


class BalanceAdminForm(forms.ModelForm):
    """
    Форма баланса с «замком»: в скрытом поле запоминаем, каким админ видел
    баланс при открытии формы. Пополнение применится, только если баланс
    с тех пор не изменился — это защита от двойного клика по «Сохранить»
    (иначе пополнение задвоится) и от гонки с параллельными возвратами.
    """
    expected_amount = forms.DecimalField(widget=forms.HiddenInput, required=False,
                                         max_digits=15, decimal_places=2)

    class Meta:
        model = Balance
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields['expected_amount'].initial = self.instance.amount


@admin.register(Balance)
class BalanceAdmin(admin.ModelAdmin):
    form = BalanceAdminForm
    list_display = ('user', 'amount', 'carry', 'updated_at')
    list_per_page = 20
    ordering = ('amount',)
    # Баланс руками не правим — только пополнение через add_amount.
    # carry (дробный остаток возвратов) меняет только billing под локом
    readonly_fields = ('amount', 'carry')
    search_fields = ('user__username', 'user__user_id',)
    search_help_text = 'USERNAME ni yoki USER ID ni kiritng'

    # Балансы пользователей может трогать только суперадмин
    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    def get_readonly_fields(self, request, obj=None):
        # Владельца существующего баланса не меняем (OneToOne к юзеру)
        if obj:
            return ('amount', 'carry', 'user')
        return ('amount', 'carry')

    def save_model(self, request, obj, form, change):
        """
        Если админ ввёл add_amount — прибавляем к балансу F-выражением прямо
        в базе (не затирая параллельные возвраты/списания), и только при
        совпадении amount с тем, что админ видел в форме (см. BalanceAdminForm).
        """
        add = obj.add_amount or 0
        expected = form.cleaned_data.get('expected_amount')

        # У юзера AdGram кошелёк в AdGram — локальный баланс не пополняем
        if add > 0 and obj.user.adgram_user_id:
            obj.add_amount = None
            if change:
                Balance.objects.filter(pk=obj.pk).update(add_amount=None)
            else:
                super().save_model(request, obj, form, change)
            self.message_user(request, 'Пополнение только в AdGram — у этого пользователя кошелёк AdGram. '
                                       'Пополнение НЕ применено.', level=messages.ERROR)
            return

        if not change:
            # Новая запись баланса (редкий случай) — сначала создаём её
            obj.add_amount = None
            super().save_model(request, obj, form, change)

        if add > 0:
            queryset = Balance.objects.filter(pk=obj.pk)
            if expected is not None:
                # «Замок»: пополняем только если баланс не изменился с момента открытия формы
                queryset = queryset.filter(amount=expected)
            with transaction.atomic():
                updated = queryset.update(
                    amount=F('amount') + add,
                    add_amount=None,
                    updated_at=timezone.now(),
                )
                if updated:
                    # Пополнение — тоже денежное движение, пишем в журнал
                    balance_after = Balance.objects.values_list('amount', flat=True).get(pk=obj.pk)
                    log_admin_deposit(obj.user, add, balance_after)
            if updated:
                self.message_user(request, f'Баланс пополнен на {add}')
            else:
                self.message_user(
                    request,
                    'Баланс изменился с момента открытия формы — пополнение НЕ применено. '
                    'Обновите страницу, проверьте сумму и повторите.',
                    level=messages.WARNING,
                )
        elif change:
            # Пополнения нет — просто сбрасываем add_amount, не переписывая amount
            Balance.objects.filter(pk=obj.pk).update(add_amount=None)


@admin.register(MoneyOperation)
class MoneyOperationAdmin(admin.ModelAdmin):
    """Журнал денежных операций — только просмотр (деньги двигает только billing)"""
    list_display = ('created_at', 'user', 'kind', 'reason', 'wallet', 'exact_amount', 'wallet_amount',
                    'status', 'attempts', 'order_type', 'order_id')
    list_filter = ('status', 'kind', 'reason', 'wallet')
    search_fields = ('id', 'user__username', 'user__adgram_username', 'order_id')
    list_per_page = 50
    list_select_related = ('user',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

