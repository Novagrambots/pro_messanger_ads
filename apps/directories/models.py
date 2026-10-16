from datetime import timedelta

from django.db import models
from django.utils import timezone


class DirectoryItem(models.Model):
    """Общие поля записи справочника: названия на трёх языках и выключатель без удаления"""
    name_en = models.CharField('Название (en)', max_length=128)
    name_ru = models.CharField('Название (ru)', max_length=128, blank=True)
    name_uz = models.CharField('Название (uz, латиница)', max_length=128, blank=True)
    is_active = models.BooleanField('Активна', default=True,
                                    help_text='Выключенную запись нельзя выбрать в таргетинге')

    class Meta:
        abstract = True

    def __str__(self):
        return self.name_en


class Language(DirectoryItem):
    """Язык — 71 код как у Telegram (en, uz, zh-hans, pt-br…)"""
    code = models.CharField('Код', max_length=16, primary_key=True)

    class Meta:
        verbose_name = 'Язык'
        verbose_name_plural = 'Языки'
        ordering = ['name_en']


class Topic(DirectoryItem):
    """Тема канала — id как у Telegram (1 Art & Design … 63 Other)"""
    id = models.PositiveSmallIntegerField('ID Telegram', primary_key=True)

    class Meta:
        verbose_name = 'Тема'
        verbose_name_plural = 'Темы'
        ordering = ['id']


class Country(DirectoryItem):
    """Страна — ISO 3166-1 alpha-2 (+ XK Косово, как у Telegram)"""
    code = models.CharField('Код', max_length=2, primary_key=True)

    class Meta:
        verbose_name = 'Страна'
        verbose_name_plural = 'Страны'
        ordering = ['name_en']


class Location(DirectoryItem):
    """Локация внутри страны (город, район, область). Область — текстом на трёх языках"""
    country = models.ForeignKey(Country, on_delete=models.PROTECT, related_name='locations', verbose_name='Страна')
    region_en = models.CharField('Область (en)', max_length=128, blank=True)
    region_ru = models.CharField('Область (ru)', max_length=128, blank=True)
    region_uz = models.CharField('Область (uz)', max_length=128, blank=True)

    class Meta:
        verbose_name = 'Локация'
        verbose_name_plural = 'Локации'
        ordering = ['country', 'name_en']


class TelegramPeer(models.Model):
    """
    Кэш данных канала / бота / группы по t.me-имени (аватар, название, подписчики, числовой id).
    Заполняет apps.directories.telegram.resolve: публичная страница t.me + Bot API (если задан токен).
    """

    class PeerType(models.TextChoices):
        CHANNEL = 'CHANNEL', 'Канал'
        BOT = 'BOT', 'Бот'
        GROUP = 'GROUP', 'Группа'
        USER = 'USER', 'Пользователь'
        UNKNOWN = 'UNKNOWN', 'Неизвестно'

    # Сколько данные считаются свежими
    FRESH_FOR = timedelta(hours=24)
    NOT_FOUND_FRESH_FOR = timedelta(hours=1)

    username = models.CharField('Username (нижний регистр)', max_length=32, unique=True)
    peer_type = models.CharField('Тип', max_length=10, choices=PeerType.choices, default=PeerType.UNKNOWN)
    # Числовой id в формате Bot API / TDLib: у каналов -100… Без токена бота — пусто
    telegram_id = models.BigIntegerField('Telegram ID', null=True, blank=True, db_index=True)
    title = models.CharField('Название', max_length=255, blank=True)
    avatar = models.ImageField('Аватар', upload_to='tg_avatars/', blank=True)
    subscribers = models.PositiveIntegerField('Подписчики / участники', null=True, blank=True)
    monthly_users = models.PositiveIntegerField('Пользователей в месяц (боты)', null=True, blank=True)
    not_found = models.BooleanField('Не найден в Telegram', default=False)
    fetched_at = models.DateTimeField('Когда получили данные')

    class Meta:
        verbose_name = 'Канал / бот Telegram'
        verbose_name_plural = 'Каналы и боты Telegram'
        ordering = ['username']

    def __str__(self):
        return f'@{self.username} ({self.get_peer_type_display()})'

    def is_fresh(self):
        ttl = self.NOT_FOUND_FRESH_FOR if self.not_found else self.FRESH_FOR
        return timezone.now() - self.fetched_at < ttl
