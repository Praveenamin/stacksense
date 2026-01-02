"""
Service latency measurement functions.

These functions measure latency for monitored services passively,
without requiring any modifications to the client servers.
"""

from django.utils import timezone
from core.models import ServiceLatencyMeasurement


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
        import time
        
        url = f"http://{server.ip_address}:{service.port}/"
        start_time = time.time()
        
        response = requests.get(
            url, 
            timeout=5,
            allow_redirects=False
        )
        
        latency_ms = (time.time() - start_time) * 1000
        
        return {
            'latency_ms': latency_ms,
            'success': response.status_code < 500,
            'status_code': response.status_code
        }
    except Exception as e:
        return {
            'latency_ms': None,
            'success': False,
            'error_message': str(e)
        }


def measure_mysql_latency(server, service):
    """
    Measure MySQL service latency by executing simple read-only query.
    Only called if service.monitoring_enabled == True.
    No modifications to client server required - uses read-only queries.
    """
    if not service.port:
        return None
    
    try:
        import mysql.connector
        import time
        
        # Attempt connection with minimal credentials
        # Note: May need read-only user configured separately
        start_time = time.time()
        
        conn = mysql.connector.connect(
            host=server.ip_address,
            port=service.port or 3306,
            user='monitoring',  # Read-only user (if configured)
            database='information_schema',
            connection_timeout=5,
            connect_timeout=5
        )
        
        cursor = conn.cursor()
        query_start = time.time()
        cursor.execute("SELECT 1")
        cursor.fetchone()
        query_time = (time.time() - query_start) * 1000
        
        conn.close()
        total_latency_ms = (time.time() - start_time) * 1000
        
        return {
            'latency_ms': total_latency_ms,
            'query_latency_ms': query_time,
            'success': True
        }
    except mysql.connector.Error as e:
        return {
            'latency_ms': None,
            'success': False,
            'error_message': f"MySQL error: {str(e)}"
        }
    except Exception as e:
        return {
            'latency_ms': None,
            'success': False,
            'error_message': str(e)
        }


def measure_service_latency(server, service):
    """
    Main function to measure latency for a service.
    Only measures if service.monitoring_enabled == True.
    Detects service type and calls appropriate measurement function.
    """
    if not service.monitoring_enabled:
        return None
    
    # Detect service type from service name or service_type
    service_name_lower = service.name.lower()
    
    if 'mysql' in service_name_lower or 'mariadb' in service_name_lower:
        result = measure_mysql_latency(server, service)
        measurement_type = 'MYSQL'
    elif 'apache' in service_name_lower or 'nginx' in service_name_lower or 'http' in service_name_lower:
        result = measure_http_latency(server, service)
        measurement_type = 'HTTP'
    else:
        # Try HTTP first (most common), fallback to MySQL if port suggests database
        if service.port in [80, 443, 8080, 8443]:
            result = measure_http_latency(server, service)
            measurement_type = 'HTTP'
        elif service.port in [3306, 3307]:
            result = measure_mysql_latency(server, service)
            measurement_type = 'MYSQL'
        else:
            # Default to HTTP for unknown services
            result = measure_http_latency(server, service)
            measurement_type = 'HTTP'
    
    if result and result.get('latency_ms') is not None:
        # Store measurement
        ServiceLatencyMeasurement.objects.create(
            service=service,
            latency_ms=result['latency_ms'],
            timestamp=timezone.now(),
            success=result.get('success', False),
            error_message=result.get('error_message'),
            measurement_type=measurement_type
        )
    
    return result

