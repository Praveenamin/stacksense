"""
Custom Password Reset Views for Stack Alert

Uses EmailAlertConfig for SMTP settings (same as alerts).
Django's built-in password reset security:
- Only sends email if user with that email exists
- Always shows "email sent" message (prevents email enumeration attacks)
"""

from django.contrib.auth import views as auth_views
from django.contrib.auth.forms import PasswordResetForm
from django.contrib.auth.models import User
from core.models import EmailAlertConfig
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import logging

logger = logging.getLogger(__name__)


class CustomPasswordResetView(auth_views.PasswordResetView):
    """
    Custom password reset that uses EmailAlertConfig for sending emails.
    
    Security behavior (inherited from Django):
    - Only sends email if a user with that email exists AND is active
    - Always shows success message regardless (prevents email enumeration)
    - Token expires after configured time (default 1 day)
    """
    
    def form_valid(self, form):
        email = form.cleaned_data["email"]
        
        # Check if email config exists (but don't reveal if email exists)
        try:
            email_config = EmailAlertConfig.objects.first()
            if not email_config or not email_config.enabled:
                logger.warning("Password reset attempted but email service not configured")
                # Still show success to prevent enumeration
                return super().form_valid(form)
            
            if not email_config.smtp_host or not email_config.from_email:
                logger.warning("Password reset attempted but email configuration incomplete")
                return super().form_valid(form)
                
        except Exception as e:
            logger.error(f"Password reset error checking config: {e}")
        
        # Call parent to handle token generation and email sending
        # Django's PasswordResetForm.save() only sends email if user exists
        return super().form_valid(form)
    
    def send_mail(self, subject_template_name, email_template_name,
                  context, from_email, to_email, html_email_template_name=None):
        """
        Send password reset email using EmailAlertConfig SMTP settings.
        
        This is called by Django's PasswordResetForm.save() ONLY if a user
        with the submitted email exists in the database.
        """
        try:
            email_config = EmailAlertConfig.objects.first()
            if not email_config or not email_config.enabled:
                logger.warning(f"Cannot send password reset to {to_email}: Email not configured")
                return
            
            # Get SMTP config (handles provider defaults)
            smtp_config = email_config.get_smtp_config()
            smtp_host = smtp_config.get('smtp_host') or email_config.smtp_host
            smtp_port = smtp_config.get('smtp_port') or email_config.smtp_port
            use_tls = smtp_config.get('use_tls', email_config.use_tls)
            use_ssl = smtp_config.get('use_ssl', email_config.use_ssl)
            
            if not smtp_host:
                logger.error("SMTP host not configured")
                return
            
            # Render email content
            from django.template.loader import render_to_string
            subject = render_to_string(subject_template_name, context).strip()
            # Remove newlines from subject
            subject = ' '.join(subject.split())
            
            # Render the email body
            body = render_to_string(email_template_name, context)
            
            # Build the email message
            msg = MIMEMultipart('alternative')
            msg['From'] = email_config.from_email
            msg['To'] = to_email
            msg['Subject'] = subject
            
            # Add plain text version
            msg.attach(MIMEText(body, 'plain'))
            
            # If HTML template provided, add HTML version
            if html_email_template_name:
                try:
                    html_body = render_to_string(html_email_template_name, context)
                    msg.attach(MIMEText(html_body, 'html'))
                except Exception:
                    pass  # Fall back to plain text only
            
            # Send via SMTP
            server = None
            try:
                if use_ssl:
                    server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30)
                else:
                    server = smtplib.SMTP(smtp_host, smtp_port, timeout=30)
                    if use_tls:
                        server.starttls()
                
                server.ehlo()
                
                # Login if credentials provided
                if email_config.username and email_config.password:
                    try:
                        server.login(email_config.username, email_config.password)
                    except smtplib.SMTPNotSupportedError:
                        logger.info("SMTP AUTH not supported, sending without authentication")
                
                server.send_message(msg)
                logger.info(f"Password reset email sent successfully to {to_email}")
                
            finally:
                if server:
                    try:
                        server.quit()
                    except Exception:
                        pass
            
        except smtplib.SMTPAuthenticationError as e:
            logger.error(f"SMTP authentication failed for password reset: {e}")
        except smtplib.SMTPException as e:
            logger.error(f"SMTP error sending password reset to {to_email}: {e}")
        except Exception as e:
            logger.error(f"Error sending password reset email to {to_email}: {e}")
