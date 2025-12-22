"""
Django signals for the core app
"""
from django.dispatch import receiver
from django.contrib.auth.signals import user_logged_in, user_login_failed
from django.utils import timezone
from .models import LoginActivity
import logging

logger = logging.getLogger(__name__)


def get_client_ip(request):
    """Extract client IP address from request"""
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0]
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip


def get_location_from_ip(ip_address):
    """
    Get location from IP address.
    This is a placeholder - in production, you might want to use a geolocation service.
    """
    # TODO: Implement IP geolocation using a service like ipapi.co, MaxMind, etc.
    # For now, return None
    return None


@receiver(user_logged_in)
def log_user_login(sender, request, user, **kwargs):
    """Log successful user login"""
    try:
        ip_address = get_client_ip(request)
        location = get_location_from_ip(ip_address)
        
        LoginActivity.objects.create(
            user=user,
            email=user.email or user.username,
            ip_address=ip_address,
            location=location,
            status=LoginActivity.StatusChoices.SUCCESS,
            timestamp=timezone.now()
        )
    except Exception as e:
        logger.error(f"Error logging user login: {str(e)}", exc_info=True)


@receiver(user_login_failed)
def log_user_login_failed(sender, credentials, request, **kwargs):
    """Log failed login attempt"""
    try:
        ip_address = get_client_ip(request)
        location = get_location_from_ip(ip_address)
        
        # Extract email/username from credentials
        email = credentials.get('username') or credentials.get('email') or 'unknown'
        
        LoginActivity.objects.create(
            user=None,  # Failed login, no user
            email=email,
            ip_address=ip_address,
            location=location,
            status=LoginActivity.StatusChoices.FAILED,
            timestamp=timezone.now()
        )
    except Exception as e:
        logger.error(f"Error logging failed login: {str(e)}", exc_info=True)
