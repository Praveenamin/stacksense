"""
Service Scanner Module - Optimized for fast, lightweight scanning
Scans servers for active services and applications using systemctl
"""
import re
import threading
from typing import Dict, List, Optional
from django.core.cache import cache
from django.utils import timezone
from datetime import timedelta


class ServiceScanner:
    """Scans servers for active services and applications"""
    
    # Common service ports and names
    SERVICE_PORTS = {
        22: "SSH",
        80: "HTTP",
        443: "HTTPS",
        3306: "MySQL",
        5432: "PostgreSQL",
        6379: "Redis",
        27017: "MongoDB",
        8080: "HTTP-Alt",
        8443: "HTTPS-Alt",
        9200: "Elasticsearch",
        3000: "Node.js",
        5000: "Flask",
        8000: "Django",
        9000: "PHP-FPM",
    }
    
    CACHE_TTL = 600  # 10 minutes cache
    
    @staticmethod
    def scan_services(server, connection=None):
        """
        Scan a server for active services using optimized systemctl commands
        Returns dict with detected services and their status
        """
        cache_key = f"services_{server.id}"
        cached_result = cache.get(cache_key)
        if cached_result:
            return cached_result
        
        services = {
            "detected_services": [],
            "active_ports": [],
            "running_processes": [],
            "installed_apps": [],
        }
        
        try:
            if connection:
                # Optimized: Single systemctl command to get all running services
                stdin, stdout, stderr = connection.exec_command(
                    "systemctl list-units --type=service --state=running --no-pager --no-legend 2>/dev/null"
                )
                systemd_output = stdout.read().decode()
                services["detected_services"] = ServiceScanner._parse_systemd_services(systemd_output)
                
                # Check listening ports (optimized)
                stdin, stdout, stderr = connection.exec_command("ss -tuln 2>/dev/null | grep LISTEN")
                ports_output = stdout.read().decode()
                services["active_ports"] = ServiceScanner._parse_ports(ports_output)
                
                # Get top processes (limited)
                stdin, stdout, stderr = connection.exec_command("ps aux --sort=-%cpu | head -11")
                processes = stdout.read().decode()
                services["running_processes"] = ServiceScanner._parse_processes(processes)
                
        except Exception as e:
            print(f"Error scanning services for {server.name}: {e}")
        
        # Cache the result
        cache.set(cache_key, services, ServiceScanner.CACHE_TTL)
        return services
    
    @staticmethod
    def scan_services_async(server, connection=None):
        """Async version using threading"""
        result = {}
        exception = None
        
        def scan():
            nonlocal result, exception
            try:
                result = ServiceScanner.scan_services(server, connection)
            except Exception as e:
                exception = e
        
        thread = threading.Thread(target=scan)
        thread.daemon = True
        thread.start()
        thread.join(timeout=10)  # 10 second timeout
        
        if exception:
            raise exception
        return result
    
    @staticmethod
    def _parse_processes(process_output: str) -> List[Dict]:
        """Parse process list output"""
        processes = []
        lines = process_output.split("\n")[1:]  # Skip header
        for line in lines[:10]:  # Limit to 10 processes
            parts = line.split()
            if len(parts) > 10:
                processes.append({
                    "name": parts[10] if len(parts) > 10 else "unknown",
                    "cpu": parts[2] if len(parts) > 2 else "0",
                    "memory": parts[3] if len(parts) > 3 else "0",
                })
        return processes
    
    @staticmethod
    def _parse_ports(ports_output: str) -> List[Dict]:
        """Parse ss output for listening ports"""
        ports = []
        for line in ports_output.split("\n"):
            if "LISTEN" in line:
                match = re.search(r":(\d+)", line)
                if match:
                    port = int(match.group(1))
                    service_name = ServiceScanner.SERVICE_PORTS.get(port, f"Port {port}")
                    ports.append({
                        "port": port,
                        "service": service_name,
                        "status": "listening"
                    })
        return ports[:10]  # Limit to 10 ports
    
    @staticmethod
    def _parse_systemd_services(services_output: str) -> List[Dict]:
        """Parse systemd service list - optimized"""
        services = []
        for line in services_output.split("\n"):
            if ".service" in line:
                parts = line.split()
                if parts:
                    service_name = parts[0].replace(".service", "")
                    status = parts[2] if len(parts) > 2 else "unknown"
                    services.append({
                        "name": service_name,
                        "status": status,
                        "type": "systemd"
                    })
        return services[:20]  # Limit to 20 services
