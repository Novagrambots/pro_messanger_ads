import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models


class CustomUser(AbstractUser):
    """Кастомная модель пользователя с UUID"""

    class Role(models.TextChoices):
        ADVERTISER = 'ADVERTISER', 'Рекламодатель'  # обычный пользователь, покупает рекламу
        MODERATOR = 'MODERATOR', 'Модератор'        # проверяет рекламу, одобряет/отклоняет

    user_id = models.UUIDField(verbose_name='User ID', default=uuid.uuid4, editable=False, unique=True)
    role = models.CharField(
        verbose_name='Роль', max_length=20,
        choices=Role.choices, default=Role.ADVERTISER,
        help_text='Роль назначает суперадмин. Каждый новый пользователь — рекламодатель',
    )
    # Числовой id пользователя в Telegram. Клиент (NovaGram) знает его, так как
    # залогинен под этим пользователем; используется для привязки/claim каналов.
    telegram_id = models.BigIntegerField(
        verbose_name='Telegram ID', null=True, blank=True, unique=True,
        help_text='Числовой id пользователя в Telegram'
    )
    # id пользователя в AdGram (cuid из его JWT). Заполнен только у тех, кто вошёл через AdGram.
    # unique уже создаёт индекс, отдельный db_index не нужен
    adgram_user_id = models.CharField(
        verbose_name='AdGram user ID', max_length=64, null=True, blank=True, unique=True,
        help_text='userId из токена AdGram'
    )
    # Читаемое имя из токена AdGram — только для отображения (в проверке доступа не участвует).
    # Обновляется при входе, если в AdGram его поменяли
    adgram_username = models.CharField(
        verbose_name='Имя в AdGram', max_length=255, blank=True, default='',
        help_text='username из токена AdGram'
    )

    class Meta:
        verbose_name = 'User'
        verbose_name_plural = 'Users'

    def __str__(self):
        return f"{self.username} ({self.user_id})"

    @property
    def is_moderator(self):
        """Может ли пользователь модерировать рекламу (модератор или суперадмин)"""
        return self.is_superuser or self.role == self.Role.MODERATOR

    def set_role(self, role):
        """Назначить роль (ADVERTISER / MODERATOR). Суперадмина так не дать: ADMIN — из токена AdGram"""
        self.role = role
        self.save(update_fields=['role'])

    @property
    def display_name(self):
        """Имя для показа на фронте: у юзера AdGram — его имя в AdGram, а не ag_<id>"""
        return self.adgram_username or self.username

    @property
    def api_role(self):
        """Роль для ответов API: суперадмин отдаётся как ADMIN, чтобы фронт его узнал"""
        return 'ADMIN' if self.is_superuser else self.role
