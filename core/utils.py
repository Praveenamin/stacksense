from django.http import Http404
from django.shortcuts import redirect
from django.contrib import messages
from .models import UserACL, EmailAlertConfig
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import logging

logger = logging.getLogger(__name__)


def has_privilege(user, privilege_key):
    """
    Check if a user has a specific privilege
    """
    if not user or not user.is_authenticated:
        return False

    # Superusers always have all privileges
    if user.is_superuser:
        return True

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


def send_email(subject, body, recipients=None):
    """
    Send email using the configured SMTP settings
    """
    try:
        config = EmailAlertConfig.objects.filter(enabled=True).first()
        if not config:
            logger.warning("No email configuration found - skipping email send")
            return False, "No email configuration found"

        smtp_config = config.get_smtp_config()
        smtp_host = smtp_config['smtp_host']
        smtp_port = smtp_config['smtp_port']
        use_tls = smtp_config['use_tls']
        use_ssl = smtp_config['use_ssl']

        # Get credentials
        username = config.username
        password = config.password  # In production, this should be decrypted
        from_email = config.from_email

        # Default recipients if not provided
        if not recipients:
            # Parse recipients from alert_recipients field if it exists
            if hasattr(config, 'alert_recipients') and config.alert_recipients:
                recipients = [r.strip() for r in config.alert_recipients.split(',')]
            else:
                recipients = []

        if not recipients:
            logger.warning("No recipients configured - skipping email send")
            return False, "No recipients configured"

        # Create message
        msg = MIMEMultipart()
        msg['From'] = from_email
        msg['To'] = ', '.join(recipients)
        msg['Subject'] = subject

        msg.attach(MIMEText(body, 'html'))

        # Send email
        if use_ssl:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port)
        else:
            server = smtplib.SMTP(smtp_host, smtp_port)
            if use_tls:
                server.starttls()

        if username and password:
            server.login(username, password)

        server.sendmail(from_email, recipients, msg.as_string())
        server.quit()

        logger.info(f"Email sent successfully to {len(recipients)} recipients")
        return True, "Email sent successfully"

    except Exception as e:
        logger.error(f"Failed to send email: {str(e)}")
        return False, f"Failed to send email: {str(e)}"


def test_email_config(config_data):
    """
    Test email configuration by sending a test email
    """
    try:
        # Create test config from data
        smtp_host = config_data['smtp_host']
        smtp_port = int(config_data['smtp_port'])
        use_tls = config_data.get('use_tls', True)
        use_ssl = config_data.get('use_ssl', False)
        username = config_data['username']
        password = config_data['password']
        from_email = config_data['from_email']
        recipients = [config_data['test_recipient']]

        # Create test message
        msg = MIMEMultipart()
        msg['From'] = from_email
        msg['To'] = ', '.join(recipients)
        msg['Subject'] = "StackWatch SMTP Test"

        msg.attach(MIMEText("This is a test email from StackWatch SMTP configuration.", 'plain'))

        # Send test email
        if use_ssl:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port)
        else:
            server = smtplib.SMTP(smtp_host, smtp_port)
            if use_tls:
                server.starttls()

        if username and password:
            server.login(username, password)

        server.sendmail(from_email, recipients, msg.as_string())
        server.quit()

        return True, "Test email sent successfully"

    except Exception as e:
        return False, f"Test email failed: {str(e)}"


