"""
Service latency measurement functions.

These functions measure latency for monitored services over the network:
- TCP latency for externally accessible services (direct from the StackSense server)
- HTTP latency for web services
Localhost-only services are skipped (not reachable without an on-host agent).
"""

import time
import socket
import logging
from django.utils import timezone
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
    
    Externally accessible services are measured directly over the network
    (TCP/HTTP). Localhost-only services are skipped (not reachable without an
    on-host agent).
    """
    if not service.monitoring_enabled:
        return None

    if not service.port:
        return None

    # Localhost-only services can't be reached from StackSense without an
    # on-host agent; skip them until agent-side latency is available.
    if is_localhost_bound(service):
        return None

    # Service is externally accessible — measure directly over the network.
    service_name_lower = service.name.lower()
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
