import math
import uuid
from decimal import Decimal

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
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
# Фото или видео в объявлении — только у сообщения в канале
MEDIA_PLACEMENTS = (Placement.CHANNEL_MESSAGE, Placement.USERS_CHANNEL_MESSAGE)

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


class ActionType(models.TextChoices):
    """Действие после перехода по рекламе: подписка на канал или запуск бота"""
    JOIN = 'JOIN', 'Подписка на канал'
    START = 'START', 'Запуск бота'


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
    # Наценки за медиа. Значения по умолчанию — по сторонним данным о Telegram.
    # «specific» — у сообщения в канале с явным списком каналов (targeting.channels)
    photo_markup_percent = models.DecimalField(
        'Наценка за фото, %', max_digits=6, decimal_places=2, default=Decimal('50'))
    video_markup_percent = models.DecimalField(
        'Наценка за видео, %', max_digits=6, decimal_places=2, default=Decimal('70'))
    photo_specific_markup_percent = models.DecimalField(
        'Наценка за фото при выборе конкретных каналов, %', max_digits=6, decimal_places=2, default=Decimal('80'))
    video_specific_markup_percent = models.DecimalField(
        'Наценка за видео при выборе конкретных каналов, %', max_digits=6, decimal_places=2, default=Decimal('100'))

    min_budget = models.PositiveIntegerField('Минимальный бюджет, сум', default=10000)
    terms_version = models.CharField('Текущая версия правил', max_length=32, default='1.0')

    # Баннер в видео: тайминги отдаёт сервер, как у Telegram (по умолчанию — рыночные цифры)
    video_start_delay = models.PositiveSmallIntegerField('Видео: первый баннер через, с', default=5)
    video_between_delay = models.PositiveSmallIntegerField('Видео: следующий баннер через, с', default=120)
    video_min_display = models.PositiveSmallIntegerField('Видео: закрыть можно через, с', default=9)
    video_max_display = models.PositiveSmallIntegerField('Видео: баннер скрывается через, с', default=30)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Цены и правила'
        verbose_name_plural = 'Цены и правила'

    def __str__(self):
        return 'Цены и правила'

    def clean(self):
        if self.video_min_display > self.video_max_display:
            raise ValidationError({'video_min_display': 'Закрыть можно не позже, чем баннер скроется сам'})

    @classmethod
    def load(cls):
        """Единственная строка настроек (создаётся с дефолтами при первом обращении)"""
        pricing, _ = cls.objects.get_or_create(pk=1)
        return pricing

    def base_cpm(self, placement):
        return getattr(self, f'min_cpm_{placement.lower()}')

    def markup_percent(self, show_channel_picture, media_type=None, specific_channels=False):
        """
        Действует одна наценка: видео → фото → фото канала → 0.
        У видео и фото — «specific», если в сообщении в канале выбраны конкретные каналы
        """
        if media_type == AdMedia.MediaType.VIDEO:
            return self.video_specific_markup_percent if specific_channels else self.video_markup_percent
        if media_type == AdMedia.MediaType.PHOTO:
            return self.photo_specific_markup_percent if specific_channels else self.photo_markup_percent
        if show_channel_picture:
            return self.channel_picture_markup_percent
        return Decimal('0')

    def min_cpm(self, placement, show_channel_picture, media_type=None, specific_channels=False):
        """Минимальный CPM объявления = базовый × (1 + наценка). CPM целый — округляем вверх до сума"""
        percent = self.markup_percent(show_channel_picture, media_type, specific_channels)
        return math.ceil(Decimal(self.base_cpm(placement)) * (1 + percent / 100))

    def min_cpm_for(self, placement, targeting, show_channel_picture, media):
        """То же по данным объявления: media — AdMedia или None, targeting — уже проверенный"""
        return self.min_cpm(placement, show_channel_picture, media.media_type if media else None,
                            _specific_channels(placement, targeting))

    def markup_percent_for(self, placement, targeting, show_channel_picture, media):
        """Наценка по данным объявления (та же, что в min_cpm_for)"""
        return self.markup_percent(show_channel_picture, media.media_type if media else None,
                                   _specific_channels(placement, targeting))

    def reprice_cpm(self, ad, show_channel_picture, media):
        """
        CPM объявления после смены креатива (как в Telegram Ads): наценка выросла — CPM вырос, упала — вернулся.
        cpm × (100 + новая) / (100 + старая): вверх до сума при росте, вниз при падении — тогда
        «добавили фото → убрали» возвращает ровно исходный CPM. Не ниже минимума нового креатива.
        Наценка не изменилась — CPM тот же (даже если цены с тех пор поднялись)
        """
        old = self.markup_percent_for(ad.placement, ad.targeting, ad.show_channel_picture, ad.media)
        new = self.markup_percent_for(ad.placement, ad.targeting, show_channel_picture, media)
        if new == old:
            return ad.cpm
        exact = Decimal(ad.cpm) * (100 + new) / (100 + old)
        cpm = math.ceil(exact) if new > old else math.floor(exact)
        return max(cpm, self.min_cpm_for(ad.placement, ad.targeting, show_channel_picture, media))


def _specific_channels(placement, targeting):
    """Сообщение в канале с явным списком каналов — у фото и видео своя («specific») наценка"""
    return placement == Placement.CHANNEL_MESSAGE and bool((targeting or {}).get('channels'))


class AdMedia(models.Model):
    """
    Фото или видео для объявления — загружается ДО создания объявления (или правки).
    Проверки — по содержимому файла (apps/ads/media.py). Видео перекодируется в фоне
    (H.264 mp4): пока PROCESSING, прикрепить нельзя. Неприкреплённые старше суток удаляет beat.
    """

    class MediaType(models.TextChoices):
        PHOTO = 'PHOTO', 'Фото'
        VIDEO = 'VIDEO', 'Видео'

    class Status(models.TextChoices):
        PROCESSING = 'PROCESSING', 'Обрабатывается'
        READY = 'READY', 'Готово'
        FAILED = 'FAILED', 'Не прошло проверку'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='ad_media',
                             verbose_name='Владелец')
    file = models.FileField('Файл', upload_to='ads/media/')
    media_type = models.CharField('Тип', max_length=8, choices=MediaType.choices)
    width = models.PositiveIntegerField('Ширина', null=True, blank=True)
    height = models.PositiveIntegerField('Высота', null=True, blank=True)
    duration = models.DecimalField('Длительность, с', max_digits=6, decimal_places=2, null=True, blank=True)
    size = models.PositiveIntegerField('Размер, байт', default=0)
    status = models.CharField('Статус', max_length=12, choices=Status.choices, default=Status.PROCESSING)
    error = models.CharField('Ошибка', max_length=255, blank=True, default='')
    created_at = models.DateTimeField('Загружено', auto_now_add=True)

    class Meta:
        verbose_name = 'Медиа объявления'
        verbose_name_plural = 'Медиа объявлений'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.get_media_type_display()} {self.id} ({self.get_status_display()})'


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
    # Что продвигаем (канал или бот) — данные из Telegram для превью и текста кнопки.
    # Пусто только у объявлений, созданных до этапа 5а
    promoted_peer = models.ForeignKey('directories.TelegramPeer', on_delete=models.PROTECT, null=True, blank=True,
                                      related_name='promoted_ads', verbose_name='Продвигаемый канал / бот')
    # Фото или видео (только у сообщения в канале); меняется через ревизию, как остальной креатив
    media = models.ForeignKey(AdMedia, on_delete=models.PROTECT, null=True, blank=True, related_name='ads',
                              verbose_name='Медиа')

    # Деньги и ставка
    cpm = models.PositiveIntegerField('CPM, сум', help_text='Цена 1000 показов, целые сумы')
    budget = models.DecimalField('Бюджет', max_digits=19, decimal_places=6, default=Decimal('0'))
    spent = models.DecimalField('Потрачено', max_digits=19, decimal_places=6, default=Decimal('0'))
    refunded = models.DecimalField('Возвращено', max_digits=19, decimal_places=6, default=Decimal('0'))
    daily_limit = models.PositiveIntegerField('Дневной лимит, сум', null=True, blank=True)
    views_per_user_per_day = models.PositiveSmallIntegerField(
        'Показов одному человеку в сутки', default=1, help_text='0 — без ограничений, иначе 1–4')

    # Итоги для списка и сортировки — увеличивает сброс показов (в той же транзакции, что и spent)
    views_total = models.PositiveBigIntegerField('Платные показы', default=0)
    free_views_total = models.PositiveBigIntegerField('Бесплатные показы', default=0)
    clicks_total = models.PositiveBigIntegerField('Клики', default=0)
    actions_total = models.PositiveBigIntegerField('Действия (подписка / запуск бота)', default=0)
    video_opens_total = models.PositiveBigIntegerField('Открытия видео', default=0)

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

    @property
    def promoted_username(self):
        return self.promoted_peer.username if self.promoted_peer else self.url.rstrip('/').rsplit('/', 1)[-1]

    @property
    def promoted_type(self):
        """CHANNEL или BOT. У объявлений до этапа 5а нет promoted_peer — по имени из ссылки"""
        from apps.directories.models import TelegramPeer
        peer = self.promoted_peer
        if peer and peer.peer_type in (TelegramPeer.PeerType.CHANNEL, TelegramPeer.PeerType.BOT):
            return peer.peer_type
        is_bot = self.promoted_username.lower().endswith('bot')
        return TelegramPeer.PeerType.BOT if is_bot else TelegramPeer.PeerType.CHANNEL

    @property
    def action_type(self):
        """Какое действие считаем: подписка на канал или запуск бота (текст переводит фронт)"""
        return ActionType.START if self.promoted_type == 'BOT' else ActionType.JOIN

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

    def _log_decision(self, moderator, action, reason='', revision=None, notify=True):
        """Запись в историю решений + уведомление владельцу"""
        AdDecision.objects.create(ad=self, revision=revision, action=action, reason=reason, moderator=moderator)
        if notify:
            # Импорт внутри — иначе циклический импорт ads <-> notifications
            from apps.notifications.services import notify_ad_decision
            notify_ad_decision(self, action, reason, revision)

    def submit(self):
        """
        Первое сохранение нового объявления (вызывается из колбэка billing.charge).
        Объявление суперадмина одобряется сразу — он сам себе модератор, как у старых заказов;
        остальные уходят на модерацию, модераторам — уведомление.
        """
        if self.user.is_superuser:
            self.status = self.initial_status
            self._set_moderation(self.user, '')
        else:
            self.status = AdStatus.IN_REVIEW
        self.save()

        if self.status == AdStatus.IN_REVIEW:
            from apps.notifications.services import notify_moderators_new_ad
            notify_moderators_new_ad(self)
        else:
            self._log_decision(self.user, AdDecision.Action.APPROVED,
                               'Объявление суперадмина одобрено автоматически', notify=False)

    def approve(self, moderator):
        """Одобрение: IN_REVIEW → статус, выбранный при создании (ACTIVE или ON_HOLD)"""
        self._require(AdStatus.IN_REVIEW, action='одобрить')
        self.status = self.initial_status
        self._set_moderation(moderator, '')
        self.save()
        self._log_decision(moderator, AdDecision.Action.APPROVED)

    def decline(self, moderator, reason):
        """Отказ: IN_REVIEW → DECLINED, весь остаток сразу возвращается. Возвращает сумму возврата"""
        self._require(AdStatus.IN_REVIEW, action='отклонить')
        refund_amount = refund(self, MoneyOperation.Reason.REJECT)
        self.status = AdStatus.DECLINED
        self._set_moderation(moderator, reason)
        self.save()
        self._log_decision(moderator, AdDecision.Action.DECLINED, reason)
        return refund_amount

    def block(self, moderator, reason):
        """Блокировка за нарушение: → BLOCKED, остаток возвращается. Возвращает сумму возврата"""
        self._require(AdStatus.IN_REVIEW, AdStatus.ACTIVE, AdStatus.ON_HOLD, AdStatus.STOPPED,
                      action='заблокировать')
        self._freeze_serving()  # сначала записать показанное — иначе вернём деньги за него
        refund_amount = refund(self, MoneyOperation.Reason.BLOCK)
        self.status = AdStatus.BLOCKED
        self._set_moderation(moderator, reason)
        self.save()
        self._supersede_pending_revision()
        self._log_decision(moderator, AdDecision.Action.BLOCKED, reason)
        return refund_amount

    def stop(self):
        """Бюджет кончился: ACTIVE → STOPPED (вызывает сброс показов, apps/ads/serving/flush.py)"""
        self._require(AdStatus.ACTIVE, action='остановить')
        self.status = AdStatus.STOPPED
        self.save(update_fields=['status', 'updated_at'])

    # Могло показываться: в Redis выдачи есть ключ бюджета и, возможно, несброшенные показы
    SERVED_STATUSES = (AdStatus.ACTIVE, AdStatus.ON_HOLD, AdStatus.STOPPED)

    def _freeze_serving(self):
        """
        Перед возвратом и пополнением: снять ключ бюджета в Redis и записать несброшенные показы
        (spent обновится). На модерации объявление ни разу не показывалось — Redis не нужен.
        Redis недоступен → ServingUnavailable (503), деньги не двигаются.
        """
        if self.status not in self.SERVED_STATUSES:
            return None
        from apps.ads.serving.flush import freeze
        return freeze(self)

    # В каких статусах можно пополнить бюджет
    BUDGET_STATUSES = (AdStatus.IN_REVIEW, AdStatus.ACTIVE, AdStatus.ON_HOLD, AdStatus.STOPPED)

    def add_budget(self, amount):
        """
        Пополнение бюджета (вызывается из колбэка billing.charge, amount = оплачено + carry).
        Остановленное из-за бюджета объявление снова крутится — без повторной модерации.
        Ключ бюджета в Redis пересчитывается из Postgres: снимается, показы записываются,
        после коммита возвращается с новым остатком.
        """
        self._require(*self.BUDGET_STATUSES, action='пополнить бюджет')
        mark = self._freeze_serving()
        self.budget += amount
        if self.status == AdStatus.STOPPED:
            self.status = AdStatus.ACTIVE
        self.save(update_fields=['budget', 'status', 'updated_at'])
        if mark:
            from apps.ads.serving.flush import restore_after_commit
            restore_after_commit(self, mark)

    def soft_delete(self):
        """Мягкое удаление в любом статусе: → DELETED, точный остаток возвращается"""
        if self.status == AdStatus.DELETED:
            raise AdStateError('Объявление уже удалено')
        self._freeze_serving()
        refund_amount = refund(self, MoneyOperation.Reason.DELETE)
        self.status = AdStatus.DELETED
        self.save(update_fields=['status', 'updated_at'])
        self._supersede_pending_revision()
        return refund_amount

    # ===================== Креатив и ревизии (только под select_for_update) =====================

    def pending_revision(self):
        return self.revisions.filter(status=AdRevision.Status.PENDING).first()

    def last_declined_revision(self):
        """Последняя ревизия, если её отклонили (после новой правки или одобрения — None)"""
        last = self.revisions.order_by('-created_at').first()
        return last if last and last.status == AdRevision.Status.DECLINED else None

    def _supersede_pending_revision(self):
        self.revisions.filter(status=AdRevision.Status.PENDING).update(status=AdRevision.Status.SUPERSEDED)

    def applies_creative_directly(self, author):
        """Правка креатива без ревизии: объявление и так на проверке или правит суперадмин"""
        return self.status == AdStatus.IN_REVIEW or author.is_superuser

    def update_creative(self, changes, author, reprice=True):
        """
        Правка креатива (text, show_channel_picture, other_info, media) — значения уже проверил сериализатор.
        Возвращает новую ревизию на проверке или None, если правка применена сразу.
        Пока ревизия на проверке, объявление крутится в прежнем виде (как у Telegram).
        reprice=False — CPM владелец прислал в том же запросе: при прямом применении не пересчитываем
        """
        if self.status in (AdStatus.DECLINED, AdStatus.BLOCKED, AdStatus.DELETED):
            raise AdStateError(f'Объявление в статусе «{self.get_status_display()}» менять нельзя')

        if self.applies_creative_directly(author):
            self.apply_creative(changes, reprice)
            return None

        # На проверке всегда одна ревизия: новая правка собирается поверх висящей и заменяет её
        pending = self.pending_revision()
        base = pending or self
        values = {field: changes.get(field, getattr(base, field)) for field in AdRevision.CREATIVE_FIELDS}
        self._supersede_pending_revision()
        revision = AdRevision.objects.create(ad=self, **values)

        from apps.notifications.services import notify_moderators_new_revision
        notify_moderators_new_revision(revision)
        return revision

    def apply_creative(self, changes, reprice=True):
        """
        Креатив вступает в силу (прямая правка или одобренная ревизия; объявление — под локом).
        Изменилась наценка (фото / видео / фото канала) — CPM пересчитывается в ту же сторону.
        Деньги не двигаются — меняется только цена показа
        """
        fields = [*changes, 'updated_at']
        if reprice:
            cpm = AdPricing.load().reprice_cpm(self, changes.get('show_channel_picture', self.show_channel_picture),
                                               changes.get('media', self.media))
            if cpm != self.cpm:
                self.cpm = cpm
                fields.append('cpm')
        for field, value in changes.items():
            setattr(self, field, value)
        self.save(update_fields=fields)

    def update_settings(self, changes):
        """
        Правка владельцем без модерации: title, cpm, daily_limit, views_per_user_per_day,
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

        for field in ('title', 'cpm', 'daily_limit', 'views_per_user_per_day', 'start_date', 'end_date',
                      'schedule'):
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


class AdRevision(models.Model):
    """
    Правка креатива объявления, ждущая модератора. Пока ревизия на проверке, объявление
    крутится в прежнем виде; одобрили — поля переносятся в объявление (статус не меняется),
    CPM пересчитывается по наценке (Ad.apply_creative). Деньги ревизии не двигают.
    На проверке у объявления всегда не больше одной ревизии.
    """

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'На проверке'
        APPROVED = 'APPROVED', 'Одобрена'
        DECLINED = 'DECLINED', 'Отклонена'
        SUPERSEDED = 'SUPERSEDED', 'Заменена'  # пришла новая правка, объявление заблокировали или удалили

    # Поля креатива, которые меняются через ревизию
    CREATIVE_FIELDS = ('text', 'show_channel_picture', 'other_info', 'media')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ad = models.ForeignKey(Ad, on_delete=models.CASCADE, related_name='revisions', verbose_name='Объявление')

    # Полный снимок креатива после правки
    text = models.CharField('Текст', max_length=160, blank=True, default='')
    show_channel_picture = models.BooleanField('Показывать фото канала', default=False)
    other_info = models.CharField('Другая информация', max_length=255, blank=True, default='')
    # Фото или видео после правки (None — медиа убрали)
    media = models.ForeignKey(AdMedia, on_delete=models.PROTECT, null=True, blank=True, related_name='revisions',
                              verbose_name='Медиа')

    status = models.CharField('Статус', max_length=12, choices=Status.choices,
                              default=Status.PENDING, db_index=True)
    reason = models.CharField('Причина отказа', max_length=500, blank=True, default='')
    moderated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                     blank=True, related_name='+', verbose_name='Кто модерировал')
    moderated_at = models.DateTimeField('Когда модерировали', null=True, blank=True)
    created_at = models.DateTimeField('Создана', auto_now_add=True)

    class Meta:
        verbose_name = 'Правка объявления'
        verbose_name_plural = 'Правки объявлений'
        ordering = ['-created_at']
        constraints = [
            # База сама не даст двум правкам одного объявления одновременно висеть на проверке
            models.UniqueConstraint(fields=['ad'], condition=Q(status='PENDING'), name='ad_one_pending_revision'),
        ]

    def __str__(self):
        return f'Правка «{self.ad.title}» ({self.get_status_display()})'

    @classmethod
    def lock_with_ad(cls, pk):
        """
        Лочит объявление, потом ревизию — в том же порядке, что и PATCH владельца
        (иначе одобрение и новая правка могли бы заблокировать друг друга).
        """
        ad_id = cls.objects.values_list('ad_id', flat=True).get(pk=pk)
        ad = Ad.objects.select_for_update().get(pk=ad_id)
        return ad, cls.objects.select_for_update().get(pk=pk)

    def _check_pending(self, ad, action):
        if self.status != self.Status.PENDING:
            raise AdStateError(f'Нельзя {action}: правка уже не на проверке ({self.get_status_display()})')
        if ad.status in (AdStatus.DECLINED, AdStatus.BLOCKED, AdStatus.DELETED):
            raise AdStateError(f'Нельзя {action}: объявление в статусе «{ad.get_status_display()}»')

    def _set_moderation(self, status, moderator, reason=''):
        self.status = status
        self.moderated_by = moderator
        self.moderated_at = timezone.now()
        self.reason = reason
        self.save(update_fields=['status', 'moderated_by', 'moderated_at', 'reason'])

    def approve(self, ad, moderator):
        """Переносит креатив в объявление (ad — взят под локом через lock_with_ad). Статус объявления тот же"""
        self._check_pending(ad, 'одобрить')
        # CPM пересчитывается от того, что стоит сейчас (наценка живого креатива → наценка правки)
        ad.apply_creative({field: getattr(self, field) for field in self.CREATIVE_FIELDS})
        self._set_moderation(self.Status.APPROVED, moderator)
        ad._log_decision(moderator, AdDecision.Action.REVISION_APPROVED, revision=self)

    def decline(self, ad, moderator, reason):
        """Отказ: объявление остаётся в прежнем виде"""
        self._check_pending(ad, 'отклонить')
        self._set_moderation(self.Status.DECLINED, moderator, reason)
        ad._log_decision(moderator, AdDecision.Action.REVISION_DECLINED, reason, revision=self)


class AdDecision(models.Model):
    """История решений модерации по объявлению и его правкам (для карточки модератора)"""

    class Action(models.TextChoices):
        APPROVED = 'APPROVED', 'Объявление одобрено'
        DECLINED = 'DECLINED', 'Объявление отклонено'
        BLOCKED = 'BLOCKED', 'Объявление заблокировано'
        REVISION_APPROVED = 'REVISION_APPROVED', 'Правка одобрена'
        REVISION_DECLINED = 'REVISION_DECLINED', 'Правка отклонена'

    ad = models.ForeignKey(Ad, on_delete=models.CASCADE, related_name='decisions', verbose_name='Объявление')
    revision = models.ForeignKey(AdRevision, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='decisions', verbose_name='Правка')
    action = models.CharField('Решение', max_length=20, choices=Action.choices)
    reason = models.CharField('Причина', max_length=500, blank=True, default='')
    moderator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                  blank=True, related_name='+', verbose_name='Модератор')
    created_at = models.DateTimeField('Когда', auto_now_add=True)

    class Meta:
        verbose_name = 'Решение модерации'
        verbose_name_plural = 'Решения модерации'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.get_action_display()} — {self.ad.title}'



class AdDailyStat(models.Model):
    """
    Статистика объявления за сутки (по Asia/Tashkent). Пишет только сброс показов из Redis
    (apps/ads/serving/flush.py): INSERT ... ON CONFLICT с прибавлением дельт.
    """
    ad = models.ForeignKey(Ad, on_delete=models.CASCADE, related_name='daily_stats', verbose_name='Объявление')
    date = models.DateField('Сутки')
    views = models.PositiveIntegerField('Платные показы', default=0)
    free_views = models.PositiveIntegerField('Бесплатные показы', default=0,
                                             help_text='Бюджет или дневной лимит кончились, объявление уже не активно')
    clicks = models.PositiveIntegerField('Клики', default=0)
    actions = models.PositiveIntegerField('Действия', default=0,
                                          help_text='Подписка на канал / запуск бота после клика')
    video_opens = models.PositiveIntegerField('Открытия видео', default=0)
    spent = models.DecimalField('Потрачено', max_digits=19, decimal_places=6, default=Decimal('0'))

    class Meta:
        verbose_name = 'Статистика за сутки'
        verbose_name_plural = 'Статистика за сутки'
        ordering = ['-date']
        constraints = [
            models.UniqueConstraint(fields=['ad', 'date'], name='ad_daily_stat_unique_day'),
        ]

    def __str__(self):
        return f'{self.ad_id} {self.date}: {self.views} показов, {self.clicks} кликов'


class FlushBatch(models.Model):
    """Записанная пачка сброса показов — чтобы одна пачка не попала в Postgres дважды"""
    id = models.UUIDField(primary_key=True)
    ads_count = models.PositiveIntegerField('Объявлений в пачке', default=0)
    created_at = models.DateTimeField('Записана', auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = 'Пачка сброса показов'
        verbose_name_plural = 'Пачки сброса показов'

    def __str__(self):
        return f'{self.id} ({self.ads_count})'


class AdReport(models.Model):
    """
    Жалоба зрителя на объявление (из приложения, POST /api/ads/report/). Одна жалоба от зрителя
    на объявление. Сырой id пользователя не храним — только viewer_key (HMAC).
    Модератор помечает жалобу просмотренной; заблокировать объявление — обычным block.
    """

    class Reason(models.TextChoices):
        SPAM = 'spam', 'Спам'
        VIOLENCE = 'violence', 'Насилие'
        PORNOGRAPHY = 'pornography', 'Порнография'
        ILLEGAL = 'illegal', 'Незаконное'
        MISLEADING = 'misleading', 'Обман'
        OTHER = 'other', 'Другое'

    class Status(models.TextChoices):
        NEW = 'NEW', 'Новая'
        REVIEWED = 'REVIEWED', 'Просмотрена'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ad = models.ForeignKey(Ad, on_delete=models.CASCADE, related_name='reports', verbose_name='Объявление')
    viewer_key = models.CharField('Зритель (HMAC)', max_length=32)
    reason = models.CharField('Причина', max_length=16, choices=Reason.choices)
    details = models.CharField('Подробности', max_length=500, blank=True, default='')
    status = models.CharField('Статус', max_length=10, choices=Status.choices, default=Status.NEW, db_index=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='+', verbose_name='Кто просмотрел')
    reviewed_at = models.DateTimeField('Когда просмотрели', null=True, blank=True)
    created_at = models.DateTimeField('Создана', auto_now_add=True)

    class Meta:
        verbose_name = 'Жалоба на объявление'
        verbose_name_plural = 'Жалобы на объявления'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=['ad', 'viewer_key'], name='ad_report_one_per_viewer'),
        ]

    def __str__(self):
        return f'{self.get_reason_display()} — {self.ad_id} ({self.get_status_display()})'

    def mark_reviewed(self, moderator):
        """Только под select_for_update: NEW → REVIEWED"""
        if self.status != self.Status.NEW:
            raise AdStateError('Жалоба уже просмотрена')
        self.status = self.Status.REVIEWED
        self.reviewed_by = moderator
        self.reviewed_at = timezone.now()
        self.save(update_fields=['status', 'reviewed_by', 'reviewed_at'])


# Столбцы таблицы объявлений (как «Customize Table» у Telegram) и включённые по умолчанию
TABLE_COLUMNS = ('views', 'opened_video', 'clicks', 'actions', 'ctr', 'cvr', 'cpm', 'cpc', 'cpa', 'spent',
                 'remaining_budget', 'targeting', 'url', 'action_type', 'status', 'created_at')
DEFAULT_TABLE_COLUMNS = ('views', 'clicks', 'actions', 'cpm', 'spent', 'remaining_budget', 'url', 'status',
                         'created_at')


class AdTableSettings(models.Model):
    """Какие столбцы таблицы объявлений показывать пользователю (и в каком порядке)"""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='ad_table_settings',
                                verbose_name='Пользователь')
    columns = ArrayField(models.CharField(max_length=32), verbose_name='Столбцы', default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Столбцы таблицы объявлений'
        verbose_name_plural = 'Столбцы таблицы объявлений'

    def __str__(self):
        return f'{self.user}: {", ".join(self.columns)}'

    @classmethod
    def columns_for(cls, user):
        saved = cls.objects.filter(user=user).values_list('columns', flat=True).first()
        return list(saved) if saved is not None else list(DEFAULT_TABLE_COLUMNS)


# Поля формы создания, которые «Create a similar ad» переносит в черновик (деньги, статусы, даты — нет)
DRAFT_COPY_FIELDS = ('placement', 'title', 'text', 'url', 'show_channel_picture', 'other_info', 'cpm', 'daily_limit',
                     'views_per_user_per_day', 'initial_status', 'schedule')


class AdDraft(models.Model):
    """
    Черновик формы создания объявления — один на пользователя, как у Telegram.
    data — содержимое формы как есть (может быть незаконченным), проверяется только при создании объявления
    """
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='ad_draft',
                                verbose_name='Пользователь')
    data = models.JSONField('Данные формы', default=dict)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Черновик объявления'
        verbose_name_plural = 'Черновики объявлений'

    def __str__(self):
        return f'Черновик {self.user}'

    @staticmethod
    def data_from_ad(ad):
        """Объявление → данные формы создания (текущий креатив, таргетинг в формате ввода)"""
        data = {field: getattr(ad, field) for field in DRAFT_COPY_FIELDS}
        data['media'] = str(ad.media_id) if ad.media_id else None
        # Каналы хранятся как {username, id}, а форма шлёт просто имена
        targeting = {key: [item['username'] if isinstance(item, dict) else item for item in value]
                     if isinstance(value, list) else value
                     for key, value in ad.targeting.items()}
        # Свой канал в исключения добавляется сам при создании. В копии ссылку часто меняют —
        # старый канал не должен остаться в исключениях
        if 'exclude_channels' in targeting:
            targeting['exclude_channels'] = [name for name in targeting['exclude_channels']
                                             if name != ad.promoted_username]
        data['targeting'] = targeting
        return data

    @classmethod
    def copy_from(cls, ad):
        """Create a similar ad: черновик пользователя заменяется копией объявления"""
        draft, _ = cls.objects.update_or_create(user=ad.user, defaults={'data': cls.data_from_ad(ad)})
        return draft
