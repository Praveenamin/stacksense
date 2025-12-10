from django import template
from django.contrib.auth.models import User
from core.models import UserACL

register = template.Library()


@register.filter
def has_privilege(user, privilege_key):
    """
    Template filter to check if a user has a specific privilege
    Usage: {% if user|has_privilege:"add_server" %}...{% endif %}
    """
    if not user or not user.is_authenticated:
        return False

    try:
        acl = UserACL.objects.get(user=user)
        return acl.has_privilege(privilege_key)
    except UserACL.DoesNotExist:
        return False


@register.simple_tag
def check_privilege(user, privilege_key):
    """
    Template tag to check if a user has a specific privilege
    Usage: {% check_privilege user "add_server" as can_add_server %}
    """
    if not user or not user.is_authenticated:
        return False

    try:
        acl = UserACL.objects.get(user=user)
        return acl.has_privilege(privilege_key)
    except UserACL.DoesNotExist:
        return False
