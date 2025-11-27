from django.contrib.auth import views as auth_views
from django.contrib.auth.forms import PasswordResetForm
from django.core.mail import send_mail
from core.models import EmailAlertConfig
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

class CustomPasswordResetView(auth_views.PasswordResetView):
    """Custom password reset that uses EmailAlertConfig for sending emails"""
    
    def form_valid(self, form):
        email = form.cleaned_data["email"]
        
        # Get email config
        try:
            email_config = EmailAlertConfig.objects.first()
            if not email_config:
                form.add_error(None, "Email service not configured. Please contact administrator.")
                return self.form_invalid(form)
            
            # Use Django's password reset form to generate the token
        except Exception as e:
            form.add_error(None, f"Error: {str(e)}")
            return self.form_invalid(form)
        
        # Call parent to handle token generation
        return super().form_valid(form)
    
    def send_mail(self, subject_template_name, email_template_name,
                  context, from_email, to_email, html_email_template_name=None):
        """Send password reset email using EmailAlertConfig"""
        try:
            email_config = EmailAlertConfig.objects.first()
            if not email_config:
                return
            
            # Get email content
            from django.template.loader import render_to_string
            subject = render_to_string(subject_template_name, context).strip()
            message = render_to_string(email_template_name, context)
            
            # Send using EmailAlertConfig settings
            from core.views import _send_alert_email
            # Actually, let's use the same SMTP connection
            import smtplib
            from email.mime.text import MIMEText
            from email.mime.multipart import MIMEMultipart
            
            msg = MIMEMultipart()
            msg['From'] = email_config.from_email
            msg['To'] = to_email
            msg['Subject'] = subject
            msg.attach(MIMEText(message, 'html'))
            
            if email_config.smtp_port == 465:
                server = smtplib.SMTP_SSL(email_config.smtp_host, email_config.smtp_port)
            else:
                server = smtplib.SMTP(email_config.smtp_host, email_config.smtp_port)
                if email_config.smtp_use_tls:
                    server.starttls()
            
            server.login(email_config.smtp_username, email_config.smtp_password)
            server.send_message(msg)
            server.quit()
            
        except Exception as e:
            print(f"Error sending password reset email: {e}")
            raise
