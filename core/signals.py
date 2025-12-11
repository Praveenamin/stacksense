from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth.models import User
from .models import UserACL


@receiver(post_save, sender=User)
def create_user_acl(sender, instance, created, **kwargs):
    """
    Automatically create UserACL entry when a new Django user is created.
    This ensures all users appear in the admin interface.
    """
    if created:
        # Create ACL entry for the new user
        UserACL.get_or_create_for_user(instance)
