"""
Service latency measurement functions.

These functions measure latency for monitored services:
- TCP latency for externally accessible services (direct from Stack Alert server)
- SSH-based local latency for localhost-bound services (executed on the server itself)
"""

import time
import socket
import paramiko
import os
import logging
from django.utils import timezone
from django.conf import settings
from core.models import ServiceLatencyMeasurement

logger = logging.getLogger(__name__)


def measure_tcp_latency(host, port, timeout=5):
    """
    Measure TCP connection latency to a host:port.
    Returns latency in milliseconds or None on failure.
    """
    try:
        start_time = time.time()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((host, port))
        sock.close()
        latency_ms = (time.time() - start_time) * 1000
        return {
            'latency_ms': round(latency_ms, 2),
            'success': True
        }
    except socket.timeout:
        return {
            'latency_ms': None,
            'success': False,
            'error_message': 'Connection timed out'
        }
    except socket.error as e:
        return {
            'latency_ms': None,
            'success': False,
            'error_message': f'Socket error: {str(e)}'
        }
    except Exception as e:
        return {
            'latency_ms': None,
            'success': False,
            'error_message': str(e)
        }


def measure_http_latency(server, service):
    """
    Measure HTTP service latency by sending health check request.
    Only called if service.monitoring_enabled == True.
    No modifications to client server required.
    """
    if not service.port:
        return None
    
    try:
        import requests
        
        # Use HTTPS for port 443, HTTP otherwise
        protocol = 'https' if service.port == 443 else 'http'
        url = f"{protocol}://{server.ip_address}:{service.port}/"
        start_time = time.time()
        
        response = requests.get(
            url, 
            timeout=5,
            allow_redirects=False,
            verify=False  # Don't verify SSL for monitoring
        )
        
        latency_ms = (time.time() - start_time) * 1000
        
        return {
            'latency_ms': round(latency_ms, 2),
            'success': response.status_code < 500,
            'status_code': response.status_code
        }
    except Exception as e:
        return {
            'latency_ms': None,
            'success': False,
            'error_message': str(e)
        }


def measure_ssh_local_latency(server, service):
    """
    Measure latency for localhost-bound services by executing
    a TCP check command ON the server itself via SSH.
    
    This is used when bind_address is 127.0.0.1 or ::1
    """
    if not service.port:
        return None
    
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    # Load SSH key
    private_key_path = getattr(settings, "SSH_PRIVATE_KEY_PATH", "/app/ssh_keys/id_rsa")
    pkey = None
    if os.path.exists(private_key_path):
        try:
            pkey = paramiko.RSAKey.from_private_key_file(private_key_path)
        except:
            pass
    
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
        
        # Python script to measure local TCP latency
        local_check_script = f'''
import socket
import time
import json

try:
    start = time.time()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(('127.0.0.1', {service.port}))
    s.close()
    latency = (time.time() - start) * 1000
    print(json.dumps({{"success": True, "latency_ms": round(latency, 2)}}))
except Exception as e:
    print(json.dumps({{"success": False, "error": str(e)}}))
'''
        
        # Execute the script via SSH
        stdin, stdout, stderr = client.exec_command(
            f'python3 -c \'{local_check_script}\'',
            timeout=15
        )
        
        output = stdout.read().decode('utf-8').strip()
        
        import json
        try:
            result = json.loads(output)
            if result.get('success'):
                return {
                    'latency_ms': result.get('latency_ms'),
                    'success': True
                }
            else:
                return {
                    'latency_ms': None,
                    'success': False,
                    'error_message': result.get('error', 'Unknown error')
                }
        except json.JSONDecodeError:
            return {
                'latency_ms': None,
                'success': False,
                'error_message': f'Invalid response: {output[:100]}'
            }
        
    except Exception as e:
        return {
            'latency_ms': None,
            'success': False,
            'error_message': f'SSH error: {str(e)}'
        }
    finally:
        client.close()


def is_localhost_bound(service):
    """Check if service is bound to localhost only"""
    if not service.bind_address:
        return False
    return service.bind_address in ('127.0.0.1', '::1', 'localhost')


def is_externally_accessible(service):
    """Check if service is externally accessible (bound to 0.0.0.0 or specific external IP)"""
    if not service.bind_address:
        return True  # Assume external if no bind address
    return service.bind_address in ('0.0.0.0', '::', '*') or not is_localhost_bound(service)


def measure_service_latency(server, service):
    """
    Main function to measure latency for a service.
    Only measures if service.monitoring_enabled == True.
    
    Uses different measurement methods based on bind address:
    - Localhost-bound (127.0.0.1): SSH-based local measurement
    - Externally accessible: Direct TCP/HTTP from Stack Alert server
    """
    if not service.monitoring_enabled:
        return None
    
    if not service.port:
        return None
    
    # Determine measurement method based on bind address
    if is_localhost_bound(service):
        # Service is localhost-only, must measure via SSH
        result = measure_ssh_local_latency(server, service)
        measurement_type = 'SSH_LOCAL'
    else:
        # Service is externally accessible
        # Detect service type from service name or port
        service_name_lower = service.name.lower()
        
        # Use HTTP for web servers
        if any(x in service_name_lower for x in ['apache', 'nginx', 'http', 'web']):
            result = measure_http_latency(server, service)
            measurement_type = 'HTTP'
        elif service.port in (80, 443, 8080, 8443):
            result = measure_http_latency(server, service)
            measurement_type = 'HTTP'
        else:
            # Use TCP for everything else (MySQL, PostgreSQL, Redis, etc.)
            result = measure_tcp_latency(server.ip_address, service.port)
            measurement_type = 'TCP'
    
    # Store measurement if we got a result
    if result:
        try:
            ServiceLatencyMeasurement.objects.create(
                service=service,
                latency_ms=result.get('latency_ms') or 0,
                timestamp=timezone.now(),
                success=result.get('success', False),
                error_message=result.get('error_message'),
                measurement_type=measurement_type
            )
        except Exception as e:
            logger.error(f"Failed to save latency measurement for {service.name}: {e}")
    
    return result


def collect_all_service_latencies(server):
    """
    Collect latency measurements for all monitored services on a server.
    Returns a list of results.
    """
    results = []
    
    from core.models import Service
    monitored_services = Service.objects.filter(
        server=server,
        monitoring_enabled=True,
        port__isnull=False
    ).exclude(port=0)
    
    for service in monitored_services:
        try:
            result = measure_service_latency(server, service)
            results.append({
                'service_id': service.id,
                'service_name': service.name,
                'port': service.port,
                'bind_address': service.bind_address,
                'result': result
            })
        except Exception as e:
            logger.error(f"Error measuring latency for {service.name} on {server.name}: {e}")
            results.append({
                'service_id': service.id,
                'service_name': service.name,
                'port': service.port,
                'bind_address': service.bind_address,
                'result': {
                    'success': False,
                    'error_message': str(e)
                }
            })
    
    return results
