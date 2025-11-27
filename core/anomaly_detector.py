import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from django.utils import timezone
from datetime import timedelta
from .models import SystemMetric, Anomaly, MonitoringConfig

try:
    from adtk.detector import ThresholdAD, PersistAD, LevelShiftAD, VolatilityShiftAD
    ADTK_AVAILABLE = True
except ImportError:
    ADTK_AVAILABLE = False


class AnomalyDetector:
    def __init__(self, server, config):
        self.server = server
        self.config = config
        self.model = None
        
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
        """Use ADTK for time-series anomaly detection"""
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
            
            # Create pandas Series for ADTK
            cpu_series = pd.Series(cpu_values, index=pd.DatetimeIndex(timestamps))
            memory_series = pd.Series(memory_values, index=pd.DatetimeIndex(timestamps))
            
            anomalies = []
            
            # Threshold detector
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
            
            # Memory threshold
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
            
            # PersistAD for sudden changes
            try:
                persist_ad = PersistAD(window=5, c=3.0)
                cpu_persist = persist_ad.detect(cpu_series)
                if not cpu_persist.empty and cpu_persist.iloc[-1]:
                    # Check if not already added
                    if not any(a["metric_type"] == "cpu" for a in anomalies):
                        anomalies.append({
                            "metric_type": "cpu",
                            "metric_name": "cpu_percent",
                            "metric_value": metric.cpu_percent,
                            "anomaly_score": 0.8,
                            "severity": Anomaly.Severity.MEDIUM,
                        })
            except:
                pass
            
            # Disk anomalies
            if metric.disk_usage:
                for mount, usage in metric.disk_usage.items():
                    if usage.get("percent", 0) > self.config.disk_threshold:
                        anomalies.append({
                            "metric_type": "disk",
                            "metric_name": f"disk_percent_{mount}",
                            "metric_value": usage.get("percent", 0),
                            "anomaly_score": 1.0,
                            "severity": self._calculate_severity(usage.get("percent", 0), self.config.disk_threshold),
                        })
            
            return anomalies
            
        except Exception as e:
            print(f"ADTK detection failed: {e}, falling back to IsolationForest")
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
