from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.directories.models import Country, Language, Location, Topic
from apps.directories.services import reset_cache


@receiver([post_save, post_delete], sender=Language)
@receiver([post_save, post_delete], sender=Topic)
@receiver([post_save, post_delete], sender=Country)
@receiver([post_save, post_delete], sender=Location)
def directory_changed(**kwargs):
    reset_cache()
