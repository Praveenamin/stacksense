import re
from django.core.management.base import BaseCommand
from django.utils import timezone
from core.models import LogEvent, AppConfig
from datetime import timedelta
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Log Troubleshooting housekeeping (remote SSH collection retired; awaiting agent-side log push)"

    def handle(self, *args, **options):
        """Remote log collection over SSH has been retired (push-agent model).

        Agent-side log shipping is not built yet, so this command no longer
        collects new log events. It still purges LogEvents past retention so the
        Log Troubleshooting tables stay bounded. The MonitoredLog config and the
        log-parsing helpers below are kept for the future agent-based pipeline.
        """
        # Purge LogEvents older than configured retention (non-SSH housekeeping).
        try:
            config = AppConfig.get_config()
            days = getattr(config, 'log_retention_days', 30) or 30
            cutoff = timezone.now() - timedelta(days=days)
            deleted, _ = LogEvent.objects.filter(last_seen__lt=cutoff).delete()
            if deleted:
                self.stdout.write(self.style.WARNING(f"Deleted {deleted} log event(s) older than {days} days."))
        except Exception as e:
            logger.warning("Log retention purge failed: %s", e)

        self.stdout.write(
            "Remote log collection over SSH has been retired; awaiting agent-side "
            "log push. No new log events were collected."
        )

    def _parse_error_lines(self, lines, monitored_log):
        """Parse log lines and extract error information"""
        errors = []
        service_type = monitored_log.service_type
        
        # Error patterns for different services
        error_patterns = {
            'apache': [
                (r'\[error\]', 'ERROR'),
                (r'\[.*:error\]', 'ERROR'),
                (r'\b500\b', 'ERROR'),
                (r'\b403\b', 'WARNING'),
                (r'\b404\b', 'INFO'),
            ],
            'nginx': [
                (r'\berror\b', 'ERROR'),
                (r'\b500\b', 'ERROR'),
                (r'\b502\b', 'ERROR'),
                (r'\b503\b', 'ERROR'),
                (r'\b403\b', 'WARNING'),
                (r'\b404\b', 'INFO'),
            ],
            'exim': [
                (r'\brejected\b', 'WARNING'),
                (r'\bfailed\b', 'ERROR'),
                (r'\berror\b', 'ERROR'),
            ],
            'postfix': [
                (r'\brejected\b', 'WARNING'),
                (r'\bfatal\b', 'ERROR'),
                (r'\berror\b', 'ERROR'),
            ],
            'mysql': [
                (r'\bERROR\b', 'ERROR'),
                (r'\bWarning\b', 'WARNING'),
            ],
            'mariadb': [
                (r'\bERROR\b', 'ERROR'),
                (r'\bWarning\b', 'WARNING'),
            ],
            'custom': [
                (r'\berror\b', 'ERROR'),
                (r'\bERROR\b', 'ERROR'),
                (r'\bfatal\b', 'ERROR'),
                (r'\bfailed\b', 'ERROR'),
                (r'\b500\b', 'ERROR'),
                (r'\b403\b', 'WARNING'),
            ],
        }
        
        patterns = error_patterns.get(service_type, error_patterns['custom'])
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Skip unwanted logs (info, debug, access logs without errors)
            if self._should_skip_line(line, service_type):
                continue
            
            # Check if line matches any error pattern
            matched_level = None
            for pattern, level in patterns:
                if re.search(pattern, line, re.IGNORECASE):
                    matched_level = level
                    break
            
            if matched_level:
                # Extract IP address if present
                ip_address = self._extract_ip_address(line)
                
                errors.append({
                    'message': line,
                    'level': matched_level,
                    'ip_address': ip_address,
                })
        
        return errors
    
    def _should_skip_line(self, line, service_type):
        """Determine if a log line should be skipped (unwanted logs)"""
        line_lower = line.lower()
        
        # Skip "File does not exist" - not useful for analysis
        if 'file does not exist' in line_lower:
            return True
        
        # Skip "client denied by server configuration" (e.g. Apache AH01630) - not useful for analysis
        if 'client denied by server configuration' in line_lower:
            return True
        
        # Skip access logs (unless they contain errors)
        if '[access]' in line_lower and 'error' not in line_lower:
            return True
        
        # Skip debug/info logs
        skip_keywords = ['[debug]', '[info]', '[notice]', 'debug:', 'info:']
        if any(keyword in line_lower for keyword in skip_keywords):
            return True
        
        # Skip successful operations
        if any(success in line_lower for success in ['200 ok', 'success', 'completed successfully']):
            if 'error' not in line_lower and 'failed' not in line_lower:
                return True
        
        return False
    
    def _normalize_message(self, message, service_type):
        """Normalize log message for deduplication (remove timestamps, varying IPs)"""
        # Remove timestamps (common formats)
        message = re.sub(r'\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}[.\d]*[Z+\-]?\d*', '[TIMESTAMP]', message)
        message = re.sub(r'\[.*?\d{2}/\w{3}/\d{4}.*?\]', '[TIMESTAMP]', message)
        message = re.sub(r'\[.*?\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}.*?\]', '[TIMESTAMP]', message)
        
        # Remove IP addresses (replace with placeholder)
        message = re.sub(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', '[IP]', message)
        
        # Remove client port in [client IP:port] / [client [IP]:port] so different connections group together
        message = re.sub(r'\[client .+?:\d+\]', '[client [IP]]', message)
        
        # Remove process/thread IDs that vary (e.g. [pid 1467919:tid 1467919] or [pid 123])
        message = re.sub(r'\[pid \d+(?::tid \d+)?\]', '[pid]', message, flags=re.IGNORECASE)
        
        # Remove request IDs/transaction IDs
        message = re.sub(r'\[.*?request.*?\d+.*?\]', '[REQUEST_ID]', message, flags=re.IGNORECASE)
        message = re.sub(r'\[.*?transaction.*?\d+.*?\]', '[TX_ID]', message, flags=re.IGNORECASE)
        
        # Apache AH01276: DirectoryIndex list can vary by version/config; normalize so same solution applies
        message = re.sub(
            r'No matching DirectoryIndex \([^)]+\)',
            'No matching DirectoryIndex ([LIST])',
            message,
            flags=re.IGNORECASE
        )
        
        return message.strip()
    
    def _extract_ip_address(self, line):
        """Extract IP address from log line"""
        # Common IP patterns in logs
        ip_pattern = r'\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b'
        matches = re.findall(ip_pattern, line)
        if matches:
            # Return first valid-looking IP (usually the client IP)
            return matches[0]
        return None










