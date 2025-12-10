from django.http import Http404
from django.shortcuts import redirect
from django.contrib import messages
from .models import UserACL


def has_privilege(user, privilege_key):
    """
    Check if a user has a specific privilege
    """
    if not user or not user.is_authenticated:
        return False

    try:
        acl = UserACL.objects.get(user=user)
        return acl.has_privilege(privilege_key)
    except UserACL.DoesNotExist:
        return False


def require_privilege(privilege_key):
    """
    Decorator to require a specific privilege for a view
    Usage: @require_privilege('add_server')
    """
    def decorator(view_func):
        def wrapper(request, *args, **kwargs):
            if not has_privilege(request.user, privilege_key):
                if request.user.is_authenticated:
                    messages.error(request, f"You don't have permission to access this feature.")
                    return redirect('monitoring_dashboard')
                else:
                    return redirect('login')
            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator


def get_user_privileges(user):
    """
    Get all privilege keys for a user
    """
    if not user or not user.is_authenticated:
        return []

    try:
        acl = UserACL.objects.get(user=user)
        return acl.get_all_privileges()
    except UserACL.DoesNotExist:
        return []
