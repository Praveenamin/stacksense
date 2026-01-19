"""
Custom Email Backend for Stack Alert

This backend reads SMTP configuration from the EmailAlertConfig model,
allowing Django's built-in email functions (like password reset) to use
the same SMTP settings configured in the Alerts Configuration page.
"""

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from django.core.mail.backends.base import BaseEmailBackend
from django.core.mail import EmailMessage
import logging

logger = logging.getLogger(__name__)


class DatabaseEmailBackend(BaseEmailBackend):
    """
    Email backend that reads SMTP settings from EmailAlertConfig model.
    Falls back to console output if no email configuration exists.
    """
    
    def __init__(self, fail_silently=False, **kwargs):
        super().__init__(fail_silently=fail_silently, **kwargs)
        self.connection = None
    
    def _get_email_config(self):
        """Get email configuration from database"""
        try:
            from core.models import EmailAlertConfig
            config = EmailAlertConfig.objects.first()
            if config:
                return config
        except Exception as e:
            logger.warning(f"Could not load EmailAlertConfig: {e}")
        return None
    
    def open(self):
        """Open SMTP connection"""
        if self.connection:
            return False
        
        config = self._get_email_config()
        if not config:
            logger.warning("No email configuration found in database")
            return False
        
        try:
            smtp_config = config.get_smtp_config()
            smtp_host = smtp_config.get('smtp_host') or config.smtp_host
            smtp_port = smtp_config.get('smtp_port') or config.smtp_port
            use_tls = smtp_config.get('use_tls', config.use_tls)
            use_ssl = smtp_config.get('use_ssl', config.use_ssl)
            
            if not smtp_host:
                logger.warning("SMTP host not configured")
                return False
            
            if use_ssl:
                self.connection = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30)
            else:
                self.connection = smtplib.SMTP(smtp_host, smtp_port, timeout=30)
                if use_tls:
                    self.connection.starttls()
            
            self.connection.ehlo()
            
            # Try to authenticate if credentials are provided
            if config.username and config.password:
                try:
                    self.connection.login(config.username, config.password)
                except smtplib.SMTPNotSupportedError:
                    # AUTH not supported (common for port 25)
                    logger.info("SMTP AUTH not supported, continuing without authentication")
                except smtplib.SMTPAuthenticationError as e:
                    logger.error(f"SMTP authentication failed: {e}")
                    if not self.fail_silently:
                        raise
                    return False
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to open SMTP connection: {e}")
            if not self.fail_silently:
                raise
            return False
    
    def close(self):
        """Close SMTP connection"""
        if self.connection:
            try:
                self.connection.quit()
            except Exception:
                pass
            self.connection = None
    
    def send_messages(self, email_messages):
        """Send one or more EmailMessage objects"""
        if not email_messages:
            return 0
        
        config = self._get_email_config()
        if not config:
            logger.warning("No email configuration found - emails will not be sent")
            # Log the emails that would have been sent
            for message in email_messages:
                logger.info(f"[EMAIL NOT SENT - No config] To: {message.to}, Subject: {message.subject}")
            return 0
        
        # Check if we have minimum required config
        smtp_config = config.get_smtp_config()
        smtp_host = smtp_config.get('smtp_host') or config.smtp_host
        
        if not smtp_host or not config.from_email:
            logger.warning("Incomplete email configuration (missing SMTP host or from_email)")
            for message in email_messages:
                logger.info(f"[EMAIL NOT SENT - Incomplete config] To: {message.to}, Subject: {message.subject}")
            return 0
        
        num_sent = 0
        new_conn_created = self.open()
        
        if not self.connection:
            logger.error("Could not establish SMTP connection")
            return 0
        
        try:
            for message in email_messages:
                try:
                    sent = self._send(message, config)
                    if sent:
                        num_sent += 1
                except Exception as e:
                    logger.error(f"Failed to send email to {message.to}: {e}")
                    if not self.fail_silently:
                        raise
        finally:
            if new_conn_created:
                self.close()
        
        return num_sent
    
    def _send(self, message, config):
        """Send a single EmailMessage"""
        try:
            # Build the email
            msg = MIMEMultipart('alternative')
            msg['Subject'] = message.subject
            msg['From'] = config.from_email
            msg['To'] = ', '.join(message.to)
            
            if message.cc:
                msg['Cc'] = ', '.join(message.cc)
            
            # Add body
            if message.body:
                # Check if it's HTML
                if hasattr(message, 'alternatives') and message.alternatives:
                    # Plain text part
                    msg.attach(MIMEText(message.body, 'plain'))
                    # HTML parts
                    for content, mimetype in message.alternatives:
                        if mimetype == 'text/html':
                            msg.attach(MIMEText(content, 'html'))
                else:
                    msg.attach(MIMEText(message.body, 'plain'))
            
            # Get all recipients
            recipients = list(message.to)
            if message.cc:
                recipients.extend(message.cc)
            if message.bcc:
                recipients.extend(message.bcc)
            
            # Send
            self.connection.sendmail(config.from_email, recipients, msg.as_string())
            logger.info(f"Email sent successfully to {message.to}")
            return True
            
        except Exception as e:
            logger.error(f"Error sending email: {e}")
            if not self.fail_silently:
                raise
            return False
