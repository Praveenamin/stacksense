"""
Anomaly Status Service

Computes and manages anomaly status summaries for servers.
This service queries unresolved anomalies and generates summary statistics
that can be cached and served via API endpoints.

Author: StackSense Development Team
"""

from django.utils import timezone
from datetime import datetime
from .models import Anomaly, Server
from .anomaly_cache import AnomalyCache


class AnomalyStatusService:
    """
    Service for computing and managing anomaly status summaries.
    
    This service provides methods to:
    - Compute anomaly summaries from database queries
    - Refresh and cache summaries
    - Determine highest severity levels
    - Flag per-metric anomaly status
    """
    
    # Severity priority order (highest to lowest)
    SEVERITY_ORDER = {
        "CRITICAL": 4,
        "HIGH": 3,
        "MEDIUM": 2,
        "LOW": 1,
        "OK": 0
    }
    
    @staticmethod
    def _get_severity_priority(severity):
        """
        Get numeric priority for severity level.
        
        Args:
            severity: Severity string (CRITICAL, HIGH, MEDIUM, LOW, OK)
        
        Returns:
            int: Priority value (higher = more severe)
        """
        return AnomalyStatusService.SEVERITY_ORDER.get(severity.upper(), 0)
    
    @staticmethod
    def _determine_highest_severity(severities):
        """
        Determine the highest severity from a list of severities.
        
        Args:
            severities: List of severity strings
        
        Returns:
            str: Highest severity level, or "OK" if list is empty
        
        Example:
            >>> AnomalyStatusService._determine_highest_severity(["LOW", "HIGH", "MEDIUM"])
            "HIGH"
        """
        if not severities:
            return "OK"
        
        # Sort by priority (highest first)
        sorted_severities = sorted(
            severities,
            key=AnomalyStatusService._get_severity_priority,
            reverse=True
        )
        
        return sorted_severities[0]
    
    @staticmethod
    def compute_summary(server):
        """
        Compute anomaly status summary for a server.
        
        Queries all unresolved anomalies for the server and computes:
        - Active anomaly count
        - Highest severity level
        - Per-metric anomaly flags (cpu, memory, disk, network)
        
        Args:
            server: Server model instance
        
        Returns:
            dict: Summary dictionary matching the exact format:
            {
                "active": int,
                "highest_severity": "OK" | "LOW" | "MEDIUM" | "HIGH" | "CRITICAL",
                "timestamp": "<ISO timestamp>",
                "details": {
                    "cpu": "normal|anomaly",
                    "memory": "normal|anomaly",
                    "disk": "normal|anomaly",
                    "network": "normal|anomaly"
                }
            }
        
        Example:
            >>> server = Server.objects.get(id=1)
            >>> summary = AnomalyStatusService.compute_summary(server)
            >>> print(summary['active'])
            2
        """
        # Query all unresolved anomalies for this server
        unresolved_anomalies = Anomaly.objects.filter(
            server=server,
            resolved=False
        ).order_by('-timestamp')
        
        # Count active anomalies
        active_count = unresolved_anomalies.count()
        
        # Initialize per-metric flags
        metric_flags = {
            "cpu": "normal",
            "memory": "normal",
            "disk": "normal",
            "network": "normal"
        }
        
        # Collect severities and flag metrics
        severities = []
        
        for anomaly in unresolved_anomalies:
            # Add severity to list
            if anomaly.severity:
                severities.append(anomaly.severity)
            
            # Flag the metric type as anomalous
            metric_type = anomaly.metric_type.lower() if anomaly.metric_type else None
            
            if metric_type in metric_flags:
                metric_flags[metric_type] = "anomaly"
            elif metric_type and metric_type.startswith("cpu"):
                metric_flags["cpu"] = "anomaly"
            elif metric_type and metric_type.startswith("memory") or metric_type == "ram":
                metric_flags["memory"] = "anomaly"
            elif metric_type and metric_type.startswith("disk"):
                metric_flags["disk"] = "anomaly"
            elif metric_type and metric_type.startswith("network"):
                metric_flags["network"] = "anomaly"
        
        # Determine highest severity
        highest_severity = AnomalyStatusService._determine_highest_severity(severities)
        
        # Generate ISO timestamp
        timestamp = timezone.now().isoformat()
        
        # Build summary dictionary
        summary = {
            "active": active_count,
            "highest_severity": highest_severity,
            "timestamp": timestamp,
            "details": metric_flags
        }
        
        return summary
    
    @staticmethod
    def refresh_and_cache(server):
        """
        Compute fresh anomaly summary and cache it in Redis.
        
        This method:
        1. Computes the current anomaly summary
        2. Saves it to Redis cache with 5-minute TTL
        3. Returns the summary
        
        Args:
            server: Server model instance
        
        Returns:
            dict: Summary dictionary (same format as compute_summary)
        
        Example:
            >>> server = Server.objects.get(id=1)
            >>> summary = AnomalyStatusService.refresh_and_cache(server)
            >>> # Summary is now cached in Redis
        """
        # Compute fresh summary
        summary = AnomalyStatusService.compute_summary(server)
        
        # Cache it
        AnomalyCache.save_status(server.id, summary)
        
        return summary

