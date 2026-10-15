import math
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models, transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.billing.models import MoneyOperation
from apps.billing.services import refund


class Placement(models.TextChoices):
    """Место показа. Формат объявления (поиск / боты / пользователи / каналы) выводится из него"""
    SEARCH = 'SEARCH', 'Поиск'
    BOT = 'BOT', 'Боты'
    USERS_CHANNEL_MESSAGE = 'USERS_CHANNEL_MESSAGE', 'Пользователи: сообщение в канале'
    USERS_VIDEO_BANNER = 'USERS_VIDEO_BANNER', 'Пользователи: баннер в видео'
    USERS_BOT_BANNER = 'USERS_BOT_BANNER', 'Пользователи: баннер в боте'
    CHANNEL_MESSAGE = 'CHANNEL_MESSAGE', 'Каналы: сообщение в канале'


USERS_PLACEMENTS = (Placement.USERS_CHANNEL_MESSAGE, Placement.USERS_VIDEO_BANNER, Placement.USERS_BOT_BANNER)

AD_FORMATS = {
    Placement.SEARCH: 'search',
    Placement.BOT: 'bot',
    Placement.USERS_CHANNEL_MESSAGE: 'users',
    Placement.USERS_VIDEO_BANNER: 'users',
    Placement.USERS_BOT_BANNER: 'users',
    Placement.CHANNEL_MESSAGE: 'channels',
}


class AdStatus(models.TextChoices):
    IN_REVIEW = 'IN_REVIEW', 'На модерации'
    ACTIVE = 'ACTIVE', 'Активно'
    ON_HOLD = 'ON_HOLD', 'На паузе'
    DECLINED = 'DECLINED', 'Отклонено'
    BLOCKED = 'BLOCKED', 'Заблокировано'
    STOPPED = 'STOPPED', 'Бюджет кончился'
    DELETED = 'DELETED', 'Удалено'


class InitialStatus(models.TextChoices):
    """Каким станет объявление после одобрения модератором"""
    ACTIVE = AdStatus.ACTIVE.value, AdStatus.ACTIVE.label
    ON_HOLD = AdStatus.ON_HOLD.value, AdStatus.ON_HOLD.label


class AdStateError(Exception):
    """Недопустимый переход статуса или действие с объявлением — вьюха отвечает 400"""

    def __init__(self, message, field=None):
        super().__init__(message)
        self.field = field  # к какому полю относится ошибка (для ответа API), если к конкретному


def default_schedule():
    """Расписание выключено. days — 7 масок по 24 бита (Пн…Вс), бит h = час h:00–h+1:00"""
    return {'enabled': False, 'days': [0] * 7, 'timezone': settings.TIME_ZONE}


class AdPricing(models.Model):
    """
    Цены и правила площадки — одна строка (pk=1), правит суперадмин в админке без правки кода.
    Наценки поднимают только МИНИМАЛЬНЫЙ CPM, ставку рекламодателя не меняют.
    """
    min_cpm_search = models.PositiveIntegerField('Мин. CPM: поиск', default=1000)
    min_cpm_bot = models.PositiveIntegerField('Мин. CPM: боты', default=1000)
    min_cpm_users_channel_message = models.PositiveIntegerField(
        'Мин. CPM: пользователи — сообщение в канале', default=1000)
    min_cpm_users_video_banner = models.PositiveIntegerField('Мин. CPM: пользователи — баннер в видео', default=1000)
    min_cpm_users_bot_banner = models.PositiveIntegerField('Мин. CPM: пользователи — баннер в боте', default=1000)
    min_cpm_channel_message = models.PositiveIntegerField('Мин. CPM: каналы — сообщение в канале', default=1000)

    channel_picture_markup_percent = models.DecimalField(
        'Наценка за фото канала, %', max_digits=6, decimal_places=2, default=Decimal('30'))
    # Применит этап 6 (медиа). Значения по умолчанию — по сторонним данным о Telegram
    photo_markup_percent = models.DecimalField(
        'Наценка за фото, %', max_digits=6, decimal_places=2, default=Decimal('50'))
    video_markup_percent = models.DecimalField(
        'Наценка за видео, %', max_digits=6, decimal_places=2, default=Decimal('70'))

    min_budget = models.PositiveIntegerField('Минимальный бюджет, сум', default=10000)
    terms_version = models.CharField('Текущая версия правил', max_length=32, default='1.0')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Цены и правила'
        verbose_name_plural = 'Цены и правила'

    def __str__(self):
        return 'Цены и правила'

    @classmethod
    def load(cls):
        """Единственная строка настроек (создаётся с дефолтами при первом обращении)"""
        pricing, _ = cls.objects.get_or_create(pk=1)
        return pricing

    def base_cpm(self, placement):
        return getattr(self, f'min_cpm_{placement.lower()}')

    def min_cpm(self, placement, show_channel_picture):
        """Минимальный CPM объявления = базовый × (1 + наценка). CPM целый — округляем вверх до сума"""
        minimum = Decimal(self.base_cpm(placement))
        if show_channel_picture:
            minimum *= 1 + self.channel_picture_markup_percent / 100
        return math.ceil(minimum)


class Ad(models.Model):
    """
    Объявление v2 — одна модель для всех форматов (поиск, боты, пользователи, каналы).

    Деньги (Decimal, 6 знаков): budget — всего внесено (включая carry), spent — потрачено,
    refunded — возвращено; остаток = budget − spent − refunded. Деньги двигает только
    apps.billing.services (charge / refund) с записью в журнал MoneyOperation.

    Все переходы статусов — методами ниже и ТОЛЬКО на объекте, взятом через
    select_for_update() внутри transaction.atomic() (иначе гонка двух запросов).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # PROTECT: на объявлении деньги — юзера с объявлениями удалить нельзя
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                             related_name='ads', verbose_name='Рекламодатель')
    placement = models.CharField('Место показа', max_length=32, choices=Placement.choices)

    # Креатив
    title = models.CharField('Название (внутреннее)', max_length=255)
    text = models.CharField('Текст', max_length=160, blank=True, default='')
    url = models.CharField('Ссылка t.me', max_length=64, help_text='https://t.me/<username>')
    show_channel_picture = models.BooleanField('Показывать фото канала', default=False)
    other_info = models.CharField('Другая информация', max_length=255, blank=True, default='')

    # Деньги и ставка
    cpm = models.PositiveIntegerField('CPM, сум', help_text='Цена 1000 показов, целые сумы')
    budget = models.DecimalField('Бюджет', max_digits=19, decimal_places=6, default=Decimal('0'))
    spent = models.DecimalField('Потрачено', max_digits=19, decimal_places=6, default=Decimal('0'))
    refunded = models.DecimalField('Возвращено', max_digits=19, decimal_places=6, default=Decimal('0'))
    daily_limit = models.PositiveIntegerField('Дневной лимит, сум', null=True, blank=True)
    views_per_user_per_day = models.PositiveSmallIntegerField(
        'Показов одному человеку в сутки', default=1, help_text='0 — без ограничений, иначе 1–4')

    # Статус, даты, расписание
    status = models.CharField('Статус', max_length=16, choices=AdStatus.choices,
                              default=AdStatus.IN_REVIEW, db_index=True)
    initial_status = models.CharField('Статус после одобрения', max_length=16,
                                      choices=InitialStatus.choices, default=InitialStatus.ACTIVE)
    start_date = models.DateTimeField('Запустить в', null=True, blank=True)
    end_date = models.DateTimeField('Поставить на паузу в', null=True, blank=True)
    schedule = models.JSONField('Расписание', default=default_schedule)

    # Таргетинг по месту показа — после создания не меняется (см. validators.validate_targeting)
    targeting = models.JSONField('Таргетинг', default=dict)

    # Согласие с правилами площадки
    terms_version = models.CharField('Версия правил', max_length=32)
    terms_accepted_at = models.DateTimeField('Когда согласился с правилами')

    # Модерация (API модераторов — этап 4)
    moderation_reason = models.CharField('Причина отказа / блокировки', max_length=500, blank=True, default='')
    moderated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                     blank=True, related_name='+', verbose_name='Кто модерировал')
    moderated_at = models.DateTimeField('Когда модерировали', null=True, blank=True)

    created_at = models.DateTimeField('Создано', auto_now_add=True)
    updated_at = models.DateTimeField('Изменено', auto_now=True)

    class Meta:
        verbose_name = 'Объявление'
        verbose_name_plural = 'Объявления'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', '-created_at']),  # список своих объявлений
            models.Index(fields=['status', 'start_date']),  # beat: пора запускать
            models.Index(fields=['status', 'end_date']),    # beat: пора на паузу
        ]
        constraints = [
            # Страховка от ошибок будущей выдачи: нельзя потратить и вернуть больше, чем внесли
            models.CheckConstraint(
                condition=Q(spent__gte=0) & Q(refunded__gte=0) & Q(spent__lte=F('budget') - F('refunded')),
                name='ad_money_within_budget'),
            models.CheckConstraint(condition=Q(cpm__gt=0), name='ad_cpm_positive'),
            models.CheckConstraint(condition=Q(views_per_user_per_day__lte=4), name='ad_views_per_user_per_day_max_4'),
        ]

    def __str__(self):
        return f'{self.title} ({self.get_placement_display()}, {self.get_status_display()})'

    @property
    def ad_format(self):
        return AD_FORMATS[self.placement]

    def remaining_budget(self):
        """Остаток бюджета: budget − spent − refunded (точно)"""
        return self.budget - self.spent - self.refunded

    def unrefunded_amount(self):
        """Для billing.services.refund: сколько ещё можно вернуть"""
        return self.remaining_budget()

    # ======================== Переходы (только под select_for_update) ========================

    def _require(self, *allowed, action):
        if self.status not in allowed:
            raise AdStateError(f'Нельзя {action}: объявление в статусе «{self.get_status_display()}»')

    def _set_moderation(self, moderator, reason):
        self.moderated_by = moderator
        self.moderated_at = timezone.now()
        self.moderation_reason = reason

    def approve(self, moderator):
        """Одобрение: IN_REVIEW → статус, выбранный при создании (ACTIVE или ON_HOLD)"""
        self._require(AdStatus.IN_REVIEW, action='одобрить')
        self.status = self.initial_status
        self._set_moderation(moderator, '')
        self.save()

    def decline(self, moderator, reason):
        """Отказ: IN_REVIEW → DECLINED, весь остаток сразу возвращается. Возвращает сумму возврата"""
        self._require(AdStatus.IN_REVIEW, action='отклонить')
        refund_amount = refund(self, MoneyOperation.Reason.REJECT)
        self.status = AdStatus.DECLINED
        self._set_moderation(moderator, reason)
        self.save()
        return refund_amount

    def block(self, moderator, reason):
        """Блокировка за нарушение: → BLOCKED, остаток возвращается. Возвращает сумму возврата"""
        self._require(AdStatus.IN_REVIEW, AdStatus.ACTIVE, AdStatus.ON_HOLD, AdStatus.STOPPED,
                      action='заблокировать')
        refund_amount = refund(self, MoneyOperation.Reason.BLOCK)
        self.status = AdStatus.BLOCKED
        self._set_moderation(moderator, reason)
        self.save()
        return refund_amount

    def stop(self):
        """Бюджет кончился: ACTIVE → STOPPED (будет вызывать выдача)"""
        self._require(AdStatus.ACTIVE, action='остановить')
        self.status = AdStatus.STOPPED
        self.save(update_fields=['status', 'updated_at'])

    # В каких статусах можно пополнить бюджет
    BUDGET_STATUSES = (AdStatus.IN_REVIEW, AdStatus.ACTIVE, AdStatus.ON_HOLD, AdStatus.STOPPED)

    def add_budget(self, amount):
        """
        Пополнение бюджета (вызывается из колбэка billing.charge, amount = оплачено + carry).
        Остановленное из-за бюджета объявление снова крутится — без повторной модерации.
        """
        self._require(*self.BUDGET_STATUSES, action='пополнить бюджет')
        self.budget += amount
        if self.status == AdStatus.STOPPED:
            self.status = AdStatus.ACTIVE
        self.save(update_fields=['budget', 'status', 'updated_at'])

    def soft_delete(self):
        """Мягкое удаление в любом статусе: → DELETED, точный остаток возвращается"""
        if self.status == AdStatus.DELETED:
            raise AdStateError('Объявление уже удалено')
        refund_amount = refund(self, MoneyOperation.Reason.DELETE)
        self.status = AdStatus.DELETED
        self.save(update_fields=['status', 'updated_at'])
        return refund_amount

    def update_settings(self, changes):
        """
        Правка владельцем без модерации: cpm, daily_limit, views_per_user_per_day,
        status (ACTIVE ↔ ON_HOLD), даты, расписание. Значения уже проверил сериализатор,
        здесь — правила, зависящие от статуса.
        """
        if self.status in (AdStatus.DECLINED, AdStatus.BLOCKED):
            raise AdStateError(f'Объявление в статусе «{self.get_status_display()}» менять нельзя')

        if 'status' in changes:
            self._require(AdStatus.ACTIVE, AdStatus.ON_HOLD, action='сменить статус')
            self.status = changes['status']
            if self.status == AdStatus.ACTIVE:
                self.start_date = None  # дата старта имеет смысл только на паузе

        for field in ('cpm', 'daily_limit', 'views_per_user_per_day', 'start_date', 'end_date', 'schedule'):
            if field in changes:
                setattr(self, field, changes[field])

        self.check_dates()
        self.save()

    def check_dates(self):
        """Дата старта — только для объявления на паузе; окончание — позже старта"""
        on_hold = self.status == AdStatus.ON_HOLD or (
            self.status == AdStatus.IN_REVIEW and self.initial_status == AdStatus.ON_HOLD)
        if self.start_date and not on_hold:
            raise AdStateError('Дата старта задаётся только для объявления на паузе (ON_HOLD)', 'start_date')
        if self.start_date and self.end_date and self.end_date <= self.start_date:
            raise AdStateError('Дата окончания должна быть позже даты старта', 'end_date')

    # ============================== Даты (beat раз в минуту) ==============================

    def apply_start_date(self, now):
        """Наступила дата старта: ON_HOLD → ACTIVE. Сработавшую дату очищаем"""
        if self.status != AdStatus.ON_HOLD or not self.start_date or self.start_date > now:
            return
        self.start_date = None
        if self.end_date and self.end_date <= now:
            self.end_date = None  # всё окно показа прошло, пока ждали — остаёмся на паузе
        else:
            self.status = AdStatus.ACTIVE
        self.save(update_fields=['status', 'start_date', 'end_date', 'updated_at'])

    def apply_end_date(self, now):
        """Наступила дата окончания: ACTIVE → ON_HOLD. Сработавшую дату очищаем"""
        if self.status != AdStatus.ACTIVE or not self.end_date or self.end_date > now:
            return
        self.status = AdStatus.ON_HOLD
        self.end_date = None
        self.save(update_fields=['status', 'end_date', 'updated_at'])

    @classmethod
    def apply_due_dates(cls, now=None):
        """Запускает и ставит на паузу объявления по их датам. Каждое — в своей транзакции под локом"""
        now = now or timezone.now()
        due = (
            (cls.objects.filter(status=AdStatus.ON_HOLD, start_date__lte=now), 'apply_start_date'),
            (cls.objects.filter(status=AdStatus.ACTIVE, end_date__lte=now), 'apply_end_date'),
        )
        for queryset, method in due:
            for pk in queryset.values_list('pk', flat=True):
                with transaction.atomic():
                    ad = cls.objects.select_for_update().get(pk=pk)
                    getattr(ad, method)(now)
