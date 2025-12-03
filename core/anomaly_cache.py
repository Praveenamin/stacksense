"""
Anomaly Cache Module

Provides Redis-based caching for anomaly status summaries.
This module handles storing and retrieving anomaly status data with a 5-minute TTL.

Author: StackSense Development Team
"""

import json
from django.core.cache import cache


class AnomalyCache:
    """
    Redis-based cache for anomaly status summaries.
    
    Provides methods to save, load, and clear anomaly status summaries
    for individual servers. All cached data has a 5-minute TTL.
    
    Cache Key Pattern: "anomaly:{server_id}:summary"
    TTL: 300 seconds (5 minutes)
    """
    
    KEY_PATTERN = "anomaly:{server_id}:summary"
    TTL_SECONDS = 300  # 5 minutes
    
    @staticmethod
    def _get_key(server_id):
        """
        Generate cache key for a server.
        
        Args:
            server_id: Integer server ID
        
        Returns:
            str: Cache key string
        """
        return AnomalyCache.KEY_PATTERN.format(server_id=server_id)
    
    @staticmethod
    def save_status(server_id, summary_dict):
        """
        Save anomaly status summary to Redis cache.
        
        Args:
            server_id: Integer server ID
            summary_dict: Dictionary containing anomaly summary data.
                         Must match the expected format:
                         {
                             "active": int,
                             "highest_severity": str,
                             "timestamp": str,
                             "details": {
                                 "cpu": str,
                                 "memory": str,
                                 "disk": str,
                                 "network": str
                             }
                         }
        
        Returns:
            bool: True if saved successfully, False otherwise
        
        Example:
            >>> summary = {
            ...     "active": 2,
            ...     "highest_severity": "HIGH",
            ...     "timestamp": "2024-01-01T12:00:00Z",
            ...     "details": {
            ...         "cpu": "anomaly",
            ...         "memory": "normal",
            ...         "disk": "anomaly",
            ...         "network": "normal"
            ...     }
            ... }
            >>> AnomalyCache.save_status(1, summary)
            True
        """
        try:
            key = AnomalyCache._get_key(server_id)
            # Convert dict to JSON string for storage
            json_data = json.dumps(summary_dict)
            # Save to cache with TTL
            cache.set(key, json_data, AnomalyCache.TTL_SECONDS)
            return True
        except Exception as e:
            # Log error but don't raise - caching failures shouldn't break the app
            import logging
            logger = logging.getLogger('core.anomaly_cache')
            logger.warning(f"Failed to save anomaly cache for server {server_id}: {e}")
            return False
    
    @staticmethod
    def load_status(server_id):
        """
        Load anomaly status summary from Redis cache.
        
        Args:
            server_id: Integer server ID
        
        Returns:
            dict: Parsed summary dictionary, or None if not found or invalid
        
        Example:
            >>> summary = AnomalyCache.load_status(1)
            >>> if summary:
            ...     print(summary['active'])
            2
        """
        try:
            key = AnomalyCache._get_key(server_id)
            cached_data = cache.get(key)
            
            if cached_data is None:
                return None
            
            # Parse JSON string back to dict
            if isinstance(cached_data, str):
                summary_dict = json.loads(cached_data)
            else:
                # If already a dict (shouldn't happen with JSON storage, but handle it)
                summary_dict = cached_data
            
            return summary_dict
        except json.JSONDecodeError as e:
            # Invalid JSON in cache - clear it and return None
            import logging
            logger = logging.getLogger('core.anomaly_cache')
            logger.warning(f"Invalid JSON in anomaly cache for server {server_id}: {e}")
            AnomalyCache.clear(server_id)
            return None
        except Exception as e:
            import logging
            logger = logging.getLogger('core.anomaly_cache')
            logger.warning(f"Failed to load anomaly cache for server {server_id}: {e}")
            return None
    
    @staticmethod
    def clear(server_id):
        """
        Clear anomaly status cache for a server.
        
        Args:
            server_id: Integer server ID
        
        Returns:
            bool: True if cleared successfully, False otherwise
        
        Example:
            >>> AnomalyCache.clear(1)
            True
        """
        try:
            key = AnomalyCache._get_key(server_id)
            cache.delete(key)
            return True
        except Exception as e:
            import logging
            logger = logging.getLogger('core.anomaly_cache')
            logger.warning(f"Failed to clear anomaly cache for server {server_id}: {e}")
            return False

