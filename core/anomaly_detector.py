import numpy as np
import pandas as pd
import logging
import json
from sklearn.ensemble import IsolationForest
from django.utils import timezone
from datetime import timedelta
from .models import SystemMetric, Anomaly, MonitoringConfig

try:
    from adtk.detector import ThresholdAD, PersistAD, LevelShiftAD, VolatilityShiftAD
    ADTK_AVAILABLE = True
except ImportError:
    ADTK_AVAILABLE = False

# Import the new ADTK Pipeline subsystem
try:
    from .adtk_pipeline import ADTKPipeline
    PIPELINE_AVAILABLE = True
except ImportError:
    PIPELINE_AVAILABLE = False

# Set up logger
logger = logging.getLogger('core.anomaly_detector')


class AnomalyDetector:
    def __init__(self, server, config):
        self.server = server
        self.config = config
        self.model = None
        
        # Initialize ADTK Pipeline subsystem if available
        # This provides enhanced detection with multiple detectors
        self.pipeline = None
        if PIPELINE_AVAILABLE and ADTK_AVAILABLE and getattr(config, 'use_adtk', True):
            try:
                self.pipeline = ADTKPipeline(server, config)
            except Exception as e:
                logger.warning(
                    f"Failed to initialize ADTK Pipeline for server {server.name}: {e}. "
                    f"Falling back to legacy ADTK detection."
                )
                self.pipeline = None
        
    def detect_anomalies(self, metric):
        """Detect anomalies in a new metric - ADTK primary, IsolationForest fallback"""
        if self.config.use_adtk and ADTK_AVAILABLE:
            return self._detect_with_adtk(metric)
        elif self.config.use_isolation_forest:
            return self._detect_with_isolation_forest(metric)
        else:
            # Default to ADTK if available, else IsolationForest
            if ADTK_AVAILABLE:
                return self._detect_with_adtk(metric)
            return self._detect_with_isolation_forest(metric)
    
    def _detect_with_adtk(self, metric):
        """
        Use ADTK for time-series anomaly detection.
        
        This method now uses the new ADTKPipeline subsystem for enhanced detection
        with multiple detectors (threshold, persist, levelshift, volatility).
        Falls back to legacy ThresholdAD-only logic if pipeline is unavailable.
        """
        try:
            # Get recent metrics for time-series analysis
            window_size = self.config.adtk_window_size
            recent_metrics = SystemMetric.objects.filter(
                server=self.server
            ).order_by("-timestamp")[:window_size]
            
            if len(recent_metrics) < 10:
                return []
            
            # Prepare time-series data
            timestamps = [m.timestamp for m in reversed(recent_metrics)]
            cpu_values = [m.cpu_percent for m in reversed(recent_metrics)]
            memory_values = [m.memory_percent for m in reversed(recent_metrics)]
            
            anomalies = []
            
            # Try using the new ADTK Pipeline subsystem first
            if self.pipeline is not None:
                try:
                    # Use pipeline for CPU detection
                    cpu_result = self.pipeline.detect(
                        values=cpu_values,
                        timestamps=timestamps,
                        detector_list=['threshold', 'persist', 'levelshift', 'volatility'],
                        metric_name='cpu'
                    )
                    
                    if cpu_result.get('latest_anomaly', False):
                        # Calculate severity using both threshold excess and pipeline scores
                        threshold_severity = self._calculate_severity(
                            metric.cpu_percent, 
                            self.config.cpu_threshold
                        )
                        
                        # Get highest anomaly score from pipeline
                        pipeline_scores = cpu_result.get('scores', {})
                        max_pipeline_score = max(pipeline_scores.values()) if pipeline_scores else 0.0
                        
                        # Use threshold severity as base, but consider pipeline confidence
                        # If pipeline detected with high confidence, use its severity
                        if max_pipeline_score > 0.5:
                            # Pipeline is confident - use threshold severity (already calculated)
                            final_severity = threshold_severity
                        else:
                            # Pipeline detected but with lower confidence - use MEDIUM
                            final_severity = Anomaly.Severity.MEDIUM
                        
                        anomalies.append({
                            "metric_type": "cpu",
                            "metric_name": "cpu_percent",
                            "metric_value": metric.cpu_percent,
                            "anomaly_score": max_pipeline_score if max_pipeline_score > 0 else 1.0,
                            "severity": final_severity,
                        })
                
                except Exception as e:
                    logger.warning(
                        f"ADTK Pipeline detection failed for CPU on server {self.server.name}: {e}. "
                        f"Falling back to legacy ThresholdAD detection."
                    )
                    # Fall through to legacy detection below
            
            # If pipeline failed or not available, use legacy ThresholdAD detection
            if not any(a.get("metric_type") == "cpu" for a in anomalies):
                try:
                    # Legacy ThresholdAD for CPU
                    cpu_series = pd.Series(cpu_values, index=pd.DatetimeIndex(timestamps))
            threshold_ad = ThresholdAD(high=self.config.cpu_threshold, low=0)
            cpu_anomalies = threshold_ad.detect(cpu_series)
            if not cpu_anomalies.empty and cpu_anomalies.iloc[-1]:
                anomalies.append({
                    "metric_type": "cpu",
                    "metric_name": "cpu_percent",
                    "metric_value": metric.cpu_percent,
                    "anomaly_score": 1.0,
                    "severity": self._calculate_severity(metric.cpu_percent, self.config.cpu_threshold),
                })
                except Exception as e:
                    logger.warning(f"Legacy CPU threshold detection failed: {e}")
            
            # Use pipeline for Memory detection
            if self.pipeline is not None:
                try:
                    memory_result = self.pipeline.detect(
                        values=memory_values,
                        timestamps=timestamps,
                        detector_list=['threshold', 'persist', 'levelshift', 'volatility'],
                        metric_name='memory'
                    )
                    
                    if memory_result.get('latest_anomaly', False):
                        threshold_severity = self._calculate_severity(
                            metric.memory_percent,
                            self.config.memory_threshold
                        )
                        
                        pipeline_scores = memory_result.get('scores', {})
                        max_pipeline_score = max(pipeline_scores.values()) if pipeline_scores else 0.0
                        
                        if max_pipeline_score > 0.5:
                            final_severity = threshold_severity
                        else:
                            final_severity = Anomaly.Severity.MEDIUM
                        
                        anomalies.append({
                            "metric_type": "memory",
                            "metric_name": "memory_percent",
                            "metric_value": metric.memory_percent,
                            "anomaly_score": max_pipeline_score if max_pipeline_score > 0 else 1.0,
                            "severity": final_severity,
                        })
                
                except Exception as e:
                    logger.warning(
                        f"ADTK Pipeline detection failed for Memory on server {self.server.name}: {e}. "
                        f"Falling back to legacy ThresholdAD detection."
                    )
            
            # Legacy Memory threshold detection if pipeline didn't detect
            if not any(a.get("metric_type") == "memory" for a in anomalies):
                try:
                    memory_series = pd.Series(memory_values, index=pd.DatetimeIndex(timestamps))
            threshold_ad = ThresholdAD(high=self.config.memory_threshold, low=0)
            memory_anomalies = threshold_ad.detect(memory_series)
            if not memory_anomalies.empty and memory_anomalies.iloc[-1]:
                anomalies.append({
                    "metric_type": "memory",
                    "metric_name": "memory_percent",
                    "metric_value": metric.memory_percent,
                    "anomaly_score": 1.0,
                    "severity": self._calculate_severity(metric.memory_percent, self.config.memory_threshold),
                })
                except Exception as e:
                    logger.warning(f"Legacy Memory threshold detection failed: {e}")
            
            # Disk anomalies - handle disk_usage (can be dict or JSON string)
            if metric.disk_usage:
                try:
                    # Parse disk_usage if it's a JSON string
                    if isinstance(metric.disk_usage, str):
                        disk_data = json.loads(metric.disk_usage)
                    else:
                        disk_data = metric.disk_usage
                    
                    # Process each disk partition
                    for mount, usage in disk_data.items():
                        if isinstance(usage, dict):
                            disk_percent = usage.get("percent", 0)
                        else:
                            # Handle legacy format if needed
                            disk_percent = usage if isinstance(usage, (int, float)) else 0
                        
                        if disk_percent > self.config.disk_threshold:
                        anomalies.append({
                            "metric_type": "disk",
                            "metric_name": f"disk_percent_{mount}",
                                "metric_value": disk_percent,
                            "anomaly_score": 1.0,
                                "severity": self._calculate_severity(disk_percent, self.config.disk_threshold),
                            })
                except Exception as e:
                    logger.warning(f"Disk anomaly detection failed: {e}")
            
            # Network I/O anomaly detection (if network_io data is available)
            if hasattr(metric, 'network_io') and metric.network_io:
                try:
                    # Parse network_io if it's a JSON string
                    if isinstance(metric.network_io, str):
                        network_data = json.loads(metric.network_io)
                    else:
                        network_data = metric.network_io
                    
                    # Calculate total throughput for each interface
                    # For now, we'll use a simple threshold-based approach
                    # Future: could use pipeline for network metrics if we collect historical data
                    for interface, io_data in network_data.items():
                        if isinstance(io_data, dict):
                            bytes_sent = io_data.get("bytes_sent", 0)
                            bytes_recv = io_data.get("bytes_recv", 0)
                            total_bytes = bytes_sent + bytes_recv
                            
                            # Simple threshold: flag if total throughput > 1GB in collection interval
                            # This is a placeholder - could be enhanced with historical analysis
                            if total_bytes > 1073741824:  # 1GB
                                anomalies.append({
                                    "metric_type": "network",
                                    "metric_name": f"network_throughput_{interface}",
                                    "metric_value": total_bytes / 1073741824.0,  # Convert to GB
                                    "anomaly_score": 0.7,
                                    "severity": Anomaly.Severity.MEDIUM,
                                })
                except Exception as e:
                    logger.warning(f"Network anomaly detection failed: {e}")
            
            # Multi-metric correlation analysis (non-breaking enhancement)
            # This enriches detection by identifying correlated anomalies across metrics
            try:
                from .correlation_engine import MultiMetricCorrelationEngine
                
                correlation_engine = MultiMetricCorrelationEngine(self.server, self.config)
                corr_result = correlation_engine.detect_correlated_anomaly()
                
                # If correlation engine detects anomaly, elevate severity if needed
                if corr_result.get("is_anomaly", False):
                    # Elevate severity for existing anomalies (do not downgrade)
                    # Correlation indicates a more serious issue
                    severity_order = {
                        "LOW": 1,
                        "MEDIUM": 2,
                        "HIGH": 3,
                        "CRITICAL": 4
                    }
                    
                    for anomaly in anomalies:
                        current_severity = anomaly.get("severity", "MEDIUM")
                        current_level = severity_order.get(current_severity, 2)
                        
                        # If current severity is below HIGH, elevate to HIGH
                        if current_level < 3:
                            anomaly["severity"] = Anomaly.Severity.HIGH
                        
                        # Attach correlation data to anomaly for context
                        anomaly["correlation"] = corr_result
                    
                    # If no anomalies were detected by ADTK but correlation found one,
                    # add a correlation-based anomaly
                    if len(anomalies) == 0:
                        # Find the metric with highest correlation score
                        per_metric_scores = corr_result.get("per_metric_scores", {})
                        max_metric = max(per_metric_scores.items(), key=lambda x: x[1]) if per_metric_scores else None
                        
                        if max_metric:
                            metric_name, score = max_metric
                            # Create a correlation-based anomaly
                            anomalies.append({
                                "metric_type": metric_name,
                                "metric_name": f"{metric_name}_correlated",
                                "metric_value": getattr(metric, f"{metric_name}_percent", 0) if hasattr(metric, f"{metric_name}_percent") else 0,
                                "anomaly_score": corr_result.get("score", 0.0),
                                "severity": Anomaly.Severity.HIGH,
                                "correlation": corr_result
                            })
            except ImportError:
                # Correlation engine not available - skip silently
                pass
            except Exception as e:
                # Correlation engine error - log but don't break detection
                logger.warning(f"Correlation engine failed for server {self.server.name}: {e}")
            
            return anomalies
            
        except Exception as e:
            logger.warning(
                f"ADTK detection failed for server {self.server.name}: {e}, "
                f"falling back to IsolationForest"
            )
            return self._detect_with_isolation_forest(metric)
    
    def _detect_with_isolation_forest(self, metric):
        """Use IsolationForest for fast anomaly detection"""
        recent_metrics = SystemMetric.objects.filter(
            server=self.server
        ).order_by("-timestamp")[:self.config.window_size]
        
        if len(recent_metrics) < 10:
            return []
        
        features = []
        for m in reversed(recent_metrics):
            max_disk = max([d.get("percent", 0) for d in m.disk_usage.values()], default=0) if m.disk_usage else 0
            features.append([
                m.cpu_percent,
                m.memory_percent,
                m.swap_percent or 0,
                max_disk,
            ])
        
        X = np.array(features)
        
        self.model = IsolationForest(
            contamination=self.config.contamination,
            random_state=42,
            n_estimators=100
        )
        self.model.fit(X)
        
        max_disk = max([d.get("percent", 0) for d in metric.disk_usage.values()], default=0) if metric.disk_usage else 0
        latest_features = np.array([[
            metric.cpu_percent,
            metric.memory_percent,
            metric.swap_percent or 0,
            max_disk,
        ]])
        
        prediction = self.model.predict(latest_features)[0]
        score = self.model.score_samples(latest_features)[0]
        
        anomalies = []
        
        if prediction == -1:
            if metric.cpu_percent > self.config.cpu_threshold:
                anomalies.append({
                    "metric_type": "cpu",
                    "metric_name": "cpu_percent",
                    "metric_value": metric.cpu_percent,
                    "anomaly_score": abs(score),
                    "severity": self._calculate_severity(metric.cpu_percent, self.config.cpu_threshold),
                })
            
            if metric.memory_percent > self.config.memory_threshold:
                anomalies.append({
                    "metric_type": "memory",
                    "metric_name": "memory_percent",
                    "metric_value": metric.memory_percent,
                    "anomaly_score": abs(score),
                    "severity": self._calculate_severity(metric.memory_percent, self.config.memory_threshold),
                })
            
            if metric.disk_usage:
                for mount, usage in metric.disk_usage.items():
                    if usage.get("percent", 0) > self.config.disk_threshold:
                        anomalies.append({
                            "metric_type": "disk",
                            "metric_name": f"disk_percent_{mount}",
                            "metric_value": usage.get("percent", 0),
                            "anomaly_score": abs(score),
                            "severity": self._calculate_severity(usage.get("percent", 0), self.config.disk_threshold),
                        })
        
        return anomalies
    
    def _calculate_severity(self, value, threshold):
        """Calculate severity based on how far above threshold"""
        excess = (value - threshold) / threshold
        if excess > 0.5:
            return Anomaly.Severity.CRITICAL
        elif excess > 0.3:
            return Anomaly.Severity.HIGH
        elif excess > 0.1:
            return Anomaly.Severity.MEDIUM
        else:
            return Anomaly.Severity.LOW
