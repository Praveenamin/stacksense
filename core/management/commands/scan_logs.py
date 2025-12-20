import re
import paramiko
import os
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.conf import settings
from core.models import MonitoredLog, LogEvent, Server
from datetime import timedelta
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Scan log files from monitored servers and detect errors (read-only, no auto-healing)"

    def handle(self, *args, **options):
        """Scan all enabled log files for errors"""
        monitored_logs = MonitoredLog.objects.filter(enabled=True).select_related('server')
        
        if not monitored_logs.exists():
            self.stdout.write(self.style.WARNING("No enabled log monitoring configurations found."))
            return
        
        self.stdout.write(f"Scanning {monitored_logs.count()} log file(s)...")
        
        for monitored_log in monitored_logs:
            try:
                if not monitored_log.server.monitoring_config.enabled:
                    continue
                
                self.stdout.write(f"Scanning {monitored_log.application_name} on {monitored_log.server.name}...")
                self._scan_log_file(monitored_log)
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"✗ Failed to scan {monitored_log.application_name} on {monitored_log.server.name}: {e}"))
                logger.error(f"Log scan error for {monitored_log}: {e}")
    
    def _scan_log_file(self, monitored_log):
        """Scan a single log file for errors"""
        server = monitored_log.server
        log_path = monitored_log.log_path
        
        # Connect via SSH
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        # Load SSH key
        private_key_path = getattr(settings, "SSH_PRIVATE_KEY_PATH", "/app/ssh_keys/id_rsa")
        pkey = None
        if os.path.exists(private_key_path):
            try:
                pkey = paramiko.RSAKey.from_private_key_file(private_key_path)
            except Exception as e:
                logger.warning(f"Failed to load SSH key: {e}")
        
        try:
            if pkey:
                client.connect(
                    hostname=server.ip_address,
                    port=server.port,
                    username=server.username,
                    pkey=pkey,
                    timeout=30,
                )
            else:
                client.connect(
                    hostname=server.ip_address,
                    port=server.port,
                    username=server.username,
                    timeout=30,
                    look_for_keys=True,
                    allow_agent=True,
                )
            
            # Determine start position
            start_offset = monitored_log.last_read_offset
            
            # If first scan or no offset, start from 1 day ago
            if start_offset == 0 or not monitored_log.last_scan_time:
                # Calculate file size 1 day ago (approximate)
                # We'll read from tail of file and work backwards
                cmd_tail = f"tail -n 1000 {log_path} 2>/dev/null || echo ''"
            else:
                # Read from last known offset
                cmd_tail = f"tail -c +{start_offset + 1} {log_path} 2>/dev/null || echo ''"
            
            stdin, stdout, stderr = client.exec_command(cmd_tail)
            new_lines = stdout.read().decode('utf-8', errors='ignore').split('\n')
            error_output = stderr.read().decode('utf-8', errors='ignore')
            
            if error_output and 'No such file' in error_output:
                self.stdout.write(self.style.WARNING(f"  Log file not found: {log_path}"))
                client.close()
                return
            
            # Get current file size for updating offset
            stdin2, stdout2, stderr2 = client.exec_command(f"wc -c < {log_path} 2>/dev/null || echo 0")
            file_size = int(stdout2.read().decode('utf-8').strip() or 0)
            
            client.close()
            
            # Parse error lines
            errors_found = self._parse_error_lines(new_lines, monitored_log)
            
            # Update LogEvent records (with deduplication)
            for error_data in errors_found:
                log_message = error_data['message']
                log_level = error_data['level']
                ip_address = error_data.get('ip_address')
                
                # Normalize message for deduplication (remove timestamps, IPs that vary)
                normalized_message = self._normalize_message(log_message, monitored_log.service_type)
                
                # Find or create LogEvent
                log_event, created = LogEvent.objects.get_or_create(
                    monitored_log=monitored_log,
                    message=normalized_message,
                    defaults={
                        'log_level': log_level,
                        'ip_address': ip_address,
                        'first_seen': timezone.now(),
                        'last_seen': timezone.now(),
                        'event_count': 1,
                    }
                )
                
                if not created:
                    # Update existing event
                    log_event.last_seen = timezone.now()
                    log_event.event_count += 1
                    log_event.save()
            
            # Update monitored_log with new offset and scan time
            monitored_log.last_read_offset = file_size
            monitored_log.last_scan_time = timezone.now()
            monitored_log.save()
            
            if errors_found:
                self.stdout.write(self.style.SUCCESS(f"  ✓ Found {len(errors_found)} error(s)"))
            else:
                self.stdout.write(f"  ✓ No errors found")
        
        except paramiko.AuthenticationException:
            self.stderr.write(self.style.ERROR(f"  ✗ SSH authentication failed for {server.name}"))
        except paramiko.SSHException as e:
            self.stderr.write(self.style.ERROR(f"  ✗ SSH error for {server.name}: {e}"))
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"  ✗ Error scanning {server.name}: {e}"))
            logger.error(f"Log scan error: {e}", exc_info=True)
    
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
        
        # Remove process IDs that vary
        message = re.sub(r'\[pid \d+\]', '[pid]', message, flags=re.IGNORECASE)
        
        # Remove request IDs/transaction IDs
        message = re.sub(r'\[.*?request.*?\d+.*?\]', '[REQUEST_ID]', message, flags=re.IGNORECASE)
        message = re.sub(r'\[.*?transaction.*?\d+.*?\]', '[TX_ID]', message, flags=re.IGNORECASE)
        
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


