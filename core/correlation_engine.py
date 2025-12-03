"""
Multi-Metric Correlation Engine

A lightweight correlation-based anomaly detection system that analyzes
relationships between CPU, memory, disk, and network metrics to identify
correlated anomalies that might be missed by single-metric detectors.

This engine uses:
- Pearson correlation for metric relationships
- Z-score normalization for anomaly scoring
- Weighted combination for final anomaly signal
- Small window sizes (<= 120 metrics) for efficiency

Designed for small deployments (4 CPU cores / 8 GB RAM).
No heavy ML models - only lightweight numpy/pandas operations.

Author: StackSense Development Team
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Any
from django.utils import timezone
from .models import SystemMetric
import json


class MultiMetricCorrelationEngine:
    """
    Lightweight multi-metric correlation engine for anomaly detection.
    
    This engine:
    1. Loads recent metrics for a server
    2. Computes correlation matrix between metrics
    3. Calculates normalized z-scores for each metric
    4. Combines scores using weighted average
    5. Flags anomalies when combined score exceeds threshold
    
    All operations are O(n) and use minimal memory.
    """
    
    def __init__(self, server, config):
        """
        Initialize the correlation engine.
        
        Args:
            server: Server model instance
            config: MonitoringConfig model instance
        
        Configuration parameters (with defaults):
            - window_size: Number of recent metrics to analyze (default: 60, max: 120)
            - weights: Per-metric weights for score combination
            - threshold_factor: Multiplier for anomaly threshold (default: 2.0)
        """
        self.server = server
        self.config = config
        
        # Load correlation parameters from config or use defaults
        # Safe attribute access - do not modify models
        base_window = getattr(config, 'window_size', 60)
        correlation_window = getattr(config, 'correlation_window_size', None)
        
        # Limit window size to 120 for performance
        self.window_size = min(
            correlation_window if correlation_window is not None else base_window,
            120
        )
        
        # Default weights for metric combination
        # CPU and memory are most important, disk and network less so
        self.weights = {
            "cpu": 0.35,
            "memory": 0.30,
            "disk": 0.20,
            "network": 0.15
        }
        
        # Threshold factor for anomaly detection
        self.threshold_factor = getattr(
            config,
            'correlation_threshold_factor',
            getattr(config, 'adtk_threshold_factor', 2.0)
        )
        
        # Check if correlation is enabled (default: True)
        self.enabled = getattr(config, 'correlation_enabled', True)
    
    def load_recent_metrics(self) -> Optional[pd.DataFrame]:
        """
        Load recent metrics for the server and convert to DataFrame.
        
        Returns:
            pandas DataFrame with columns: ['cpu', 'memory', 'disk', 'network']
            Returns None if insufficient data (< 10 metrics)
        
        Notes:
            - Disk: Uses maximum partition percent across all partitions
            - Network: Computes delta throughput (bytes_recv and bytes_sent deltas)
            - All values are converted to float arrays
        """
        # Query recent metrics
        recent_metrics = SystemMetric.objects.filter(
            server=self.server
        ).order_by('-timestamp')[:self.window_size]
        
        if len(recent_metrics) < 10:
            # Need at least 10 metrics for meaningful correlation
            return None
        
        # Reverse for chronological order (oldest first)
        recent_metrics = list(reversed(recent_metrics))
        
        # Extract metric arrays
        cpu_values = []
        memory_values = []
        disk_values = []
        network_values = []
        
        # Track previous network values for delta calculation
        prev_net_recv = None
        prev_net_sent = None
        
        for metric in recent_metrics:
            # CPU
            cpu_values.append(float(metric.cpu_percent or 0.0))
            
            # Memory
            memory_values.append(float(metric.memory_percent or 0.0))
            
            # Disk - find maximum partition percent
            max_disk = 0.0
            if metric.disk_usage:
                try:
                    # Parse disk_usage (can be JSON string or dict)
                    if isinstance(metric.disk_usage, str):
                        disk_data = json.loads(metric.disk_usage)
                    else:
                        disk_data = metric.disk_usage
                    
                    # Find maximum percent across all partitions
                    for mount, usage in disk_data.items():
                        if isinstance(usage, dict):
                            percent = usage.get("percent", 0.0)
                        else:
                            percent = float(usage) if isinstance(usage, (int, float)) else 0.0
                        max_disk = max(max_disk, float(percent))
                except (json.JSONDecodeError, TypeError, ValueError):
                    max_disk = 0.0
            
            disk_values.append(max_disk)
            
            # Network - compute delta throughput
            net_value = 0.0
            if metric.network_io:
                try:
                    # Parse network_io (can be JSON string or dict)
                    if isinstance(metric.network_io, str):
                        net_data = json.loads(metric.network_io)
                    else:
                        net_data = metric.network_io
                    
                    # Sum bytes across all interfaces
                    total_recv = 0
                    total_sent = 0
                    
                    for interface, io_data in net_data.items():
                        if isinstance(io_data, dict):
                            total_recv += io_data.get("bytes_recv", 0) or 0
                            total_sent += io_data.get("bytes_sent", 0) or 0
                    
                    # Compute delta (change from previous measurement)
                    if prev_net_recv is not None and prev_net_sent is not None:
                        delta_recv = max(0, total_recv - prev_net_recv)
                        delta_sent = max(0, total_sent - prev_net_sent)
                        # Use maximum of in/out for network metric
                        net_value = max(delta_recv, delta_sent) / (1024 * 1024)  # Convert to MB
                    else:
                        net_value = 0.0
                    
                    prev_net_recv = total_recv
                    prev_net_sent = total_sent
                except (json.JSONDecodeError, TypeError, ValueError):
                    net_value = 0.0
            
            network_values.append(net_value)
        
        # Create DataFrame
        # All arrays must be same length
        min_length = min(len(cpu_values), len(memory_values), len(disk_values), len(network_values))
        
        if min_length < 10:
            return None
        
        # Trim to same length
        df = pd.DataFrame({
            'cpu': cpu_values[:min_length],
            'memory': memory_values[:min_length],
            'disk': disk_values[:min_length],
            'network': network_values[:min_length]
        })
        
        return df
    
    def compute_correlation_matrix(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Compute Pearson correlation matrix for metrics.
        
        Args:
            df: DataFrame with columns ['cpu', 'memory', 'disk', 'network']
        
        Returns:
            4x4 correlation matrix as DataFrame
        
        Performance:
            O(n) operation - very lightweight
            Uses pandas built-in Pearson correlation
        """
        if df is None or df.empty:
            return pd.DataFrame()
        
        # Compute Pearson correlation
        # This is O(n) and very fast
        corr_matrix = df.corr(method='pearson')
        
        return corr_matrix
    
    def compute_normalized_scores(self, df: pd.DataFrame) -> Dict[str, np.ndarray]:
        """
        Compute normalized z-scores for each metric.
        
        Z-scores measure how many standard deviations a value is from the mean.
        High absolute z-scores indicate anomalies.
        
        Args:
            df: DataFrame with metric columns
        
        Returns:
            Dictionary with arrays of anomaly scores:
            {
                "cpu_scores": [...],
                "memory_scores": [...],
                "disk_scores": [...],
                "network_scores": [...]
            }
        
        Process:
            1. Compute z-score: z = (value - mean) / std
            2. Clip extreme values to [-5, 5] to prevent outliers from dominating
            3. Convert to positive anomaly scores: abs(z)
        """
        if df is None or df.empty:
            return {
                "cpu_scores": np.array([]),
                "memory_scores": np.array([]),
                "disk_scores": np.array([]),
                "network_scores": np.array([])
            }
        
        scores = {}
        
        for metric in ['cpu', 'memory', 'disk', 'network']:
            if metric not in df.columns:
                scores[f"{metric}_scores"] = np.array([])
                continue
            
            values = df[metric].values
            
            # Compute mean and std
            mean_val = np.mean(values)
            std_val = np.std(values)
            
            # Avoid division by zero
            if std_val == 0:
                # All values are the same - no anomaly
                scores[f"{metric}_scores"] = np.zeros_like(values)
                continue
            
            # Compute z-scores
            z_scores = (values - mean_val) / std_val
            
            # Clip extreme values to prevent outliers from dominating
            z_scores = np.clip(z_scores, -5, 5)
            
            # Convert to positive anomaly scores
            anomaly_scores = np.abs(z_scores)
            
            scores[f"{metric}_scores"] = anomaly_scores
        
        return scores
    
    def compute_weighted_score(
        self,
        scores: Dict[str, np.ndarray],
        weights: Dict[str, float]
    ) -> float:
        """
        Compute weighted combination of per-metric scores.
        
        Args:
            scores: Dictionary of score arrays
            weights: Dictionary of metric weights
        
        Returns:
            Final combined anomaly score (float)
        
        Process:
            1. Normalize each metric's scores to 0-1 range
            2. Take the latest (last) score from each metric
            3. Weighted sum: combined = Σ(score[m] * weight[m])
        """
        if not scores or not weights:
            return 0.0
        
        combined_score = 0.0
        
        for metric in ['cpu', 'memory', 'disk', 'network']:
            score_key = f"{metric}_scores"
            weight_key = metric
            
            if score_key not in scores or weight_key not in weights:
                continue
            
            score_array = scores[score_key]
            
            if len(score_array) == 0:
                continue
            
            # Get latest score (last element)
            latest_score = float(score_array[-1])
            
            # Normalize to 0-1 range (divide by max if max > 0)
            max_score = np.max(score_array) if len(score_array) > 0 else 1.0
            if max_score > 0:
                normalized_score = latest_score / max_score
            else:
                normalized_score = 0.0
            
            # Add weighted contribution
            weight = weights[weight_key]
            combined_score += normalized_score * weight
        
        return float(combined_score)
    
    def detect_correlated_anomaly(self) -> Dict[str, Any]:
        """
        Main detection method - runs the complete correlation analysis.
        
        Returns:
            Dictionary with detection results:
            {
                "is_anomaly": bool,
                "score": float,  # Combined weighted score
                "correlation": dict,  # Correlation matrix as dict
                "per_metric_scores": dict  # Latest scores per metric
            }
        
        Process:
            1. Load recent metrics → DataFrame
            2. Compute correlation matrix
            3. Compute z-score anomaly scores
            4. Compute weighted final score
            5. Compare against threshold
        """
        # Check if correlation is enabled
        if not self.enabled:
            return {"is_anomaly": False}
        
        try:
            # Step 1: Load recent metrics
            df = self.load_recent_metrics()
            
            if df is None or df.empty:
                return {"is_anomaly": False}
            
            # Step 2: Compute correlation matrix
            corr_matrix = self.compute_correlation_matrix(df)
            
            # Convert correlation matrix to dict for JSON serialization
            corr_dict = {}
            if not corr_matrix.empty:
                corr_dict = corr_matrix.to_dict()
            
            # Step 3: Compute normalized z-scores
            scores = self.compute_normalized_scores(df)
            
            # Step 4: Compute weighted final score
            final_score = self.compute_weighted_score(scores, self.weights)
            
            # Step 5: Check against threshold
            threshold = self.threshold_factor
            
            # Extract latest per-metric scores for reporting
            per_metric_scores = {}
            for metric in ['cpu', 'memory', 'disk', 'network']:
                score_key = f"{metric}_scores"
                if score_key in scores and len(scores[score_key]) > 0:
                    per_metric_scores[metric] = float(scores[score_key][-1])
                else:
                    per_metric_scores[metric] = 0.0
            
            # Determine if anomaly
            is_anomaly = final_score > threshold
            
            result = {
                "is_anomaly": is_anomaly,
                "score": final_score,
                "correlation": corr_dict,
                "per_metric_scores": per_metric_scores
            }
            
            # Clean up - explicitly delete large objects to free memory
            del df
            del corr_matrix
            del scores
            
            return result
            
        except Exception as e:
            # On any error, return no anomaly (fail-safe)
            # Log error but don't break the detection pipeline
            import logging
            logger = logging.getLogger('core.correlation_engine')
            logger.warning(f"Correlation engine error for server {self.server.name}: {e}")
            return {"is_anomaly": False}

