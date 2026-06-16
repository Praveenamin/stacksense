import numpy as np
import pandas as pd
import logging
import json
from sklearn.ensemble import IsolationForest
from django.utils import timezone
from datetime import timedelta
from .models import SystemMetric, Anomaly, MonitoringConfig
from .mount_filters import is_ephemeral_mount

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
    # Robust-z (MAD-based) thresholds per sensitivity level. Higher k = less sensitive.
    SENSITIVITY_K = {"LOW": 5.0, "BALANCED": 3.5, "HIGH": 2.5}
    MIN_BASELINE_POINTS = 20      # need at least this much history before statistical flags
    BASELINE_WINDOW = 240         # trailing metrics used to build the baseline
    # Incident-grade gates for the baseline branch — keep a deviation from firing unless it is
    # large, genuinely high, AND sustained (idle servers spike to 20-30% all the time; that is
    # normal background activity, not an incident).
    MIN_ABS_DELTA = 25.0          # pp above the VM's normal before a deviation counts
    ABS_VALUE_FLOOR = {"cpu": 50.0, "memory": 60.0, "disk": 50.0}  # value must also be this high
    SUSTAIN_SAMPLES = 3           # the elevation must persist this many samples (no single blips)

    def __init__(self, server, config):
        self.server = server
        self.config = config
        self.model = None
        self.pipeline = None  # legacy ADTK pipeline retained but no longer used

    def _sensitivity(self):
        return (getattr(self.config, "anomaly_sensitivity", "BALANCED") or "BALANCED").upper()

    def detect_anomalies(self, metric):
        """
        Transparent baseline detector.

        For CPU / memory / each monitored disk%: a value is flagged when it crosses its
        configured hard ceiling, OR when it deviates *upward* from the server's own recent
        baseline (robust median + MAD) by more than the sensitivity-derived number of
        robust sigmas. Network I/O is ceiling-only for now. Every anomaly carries a
        deterministic, human-readable explanation — no LLM required.
        """
        if self._sensitivity() == "OFF":
            return []

        anomalies = []

        # Strictly-trailing history (before this metric) used to build baselines.
        history = list(
            SystemMetric.objects.filter(server=self.server, timestamp__lt=metric.timestamp)
            .order_by("-timestamp")[: self.BASELINE_WINDOW]
        )

        # CPU
        a = self._baseline_anomaly(
            "cpu", "cpu_percent", "CPU", metric.cpu_percent,
            self.config.cpu_threshold, [m.cpu_percent for m in history],
        )
        if a:
            a["explanation"] += self._top_process_suffix(metric, "cpu")
            anomalies.append(a)

        # Memory
        a = self._baseline_anomaly(
            "memory", "memory_percent", "Memory", metric.memory_percent,
            self.config.memory_threshold, [m.memory_percent for m in history],
        )
        if a:
            a["explanation"] += self._top_process_suffix(metric, "memory")
            anomalies.append(a)

        # Disk (per monitored mount)
        current_disks = self._disk_percents(metric)
        monitored = self.config.monitored_disks or []
        for mount, value in current_disks.items():
            if monitored and mount not in monitored:
                continue
            mount_history = []
            for m in history:
                dp = self._disk_percents(m)
                if mount in dp:
                    mount_history.append(dp[mount])
            a = self._baseline_anomaly(
                "disk", f"disk_percent_{mount}", f"Disk {mount}", value,
                self.config.disk_threshold, mount_history,
            )
            if a:
                anomalies.append(a)

        # Network I/O (ceiling-only)
        net = self._network_ceiling(metric)
        if net:
            anomalies.append(net)

        return anomalies

    def _top_process_suffix(self, metric, kind):
        """A ' Top process at that time: <name> (pid N) at X% CPU/memory.' clause built
        from the metric's own captured top_processes -- so a CPU/memory anomaly names the
        culprit that was heaviest at that exact sample (mirrors the leak-detector style).
        Returns '' if no process data was captured."""
        try:
            tp = metric.top_processes
            if isinstance(tp, str):
                tp = json.loads(tp)
            rows = (tp or {}).get(kind) or []
            if not rows:
                return ""
            p = rows[0]                       # agent sorts each list heaviest-first
            name = p.get("name") or "unknown"
            pid = p.get("pid")
            field = "cpu_percent" if kind == "cpu" else "memory_percent"
            unit = "CPU" if kind == "cpu" else "memory"
            val = p.get(field)
            pid_str = f" (pid {pid})" if pid else ""
            val_str = f" at {val:.0f}% {unit}" if isinstance(val, (int, float)) else ""
            return f" Top process at that time: {name}{pid_str}{val_str}."
        except Exception:
            return ""

    def _baseline_anomaly(self, metric_type, metric_name, label, value, threshold, history):
        """Return an anomaly dict for `value` (hard ceiling OR robust-z baseline), else None."""
        if value is None:
            return None
        value = float(value)
        k = self.SENSITIVITY_K[self._sensitivity()]

        # 1) Hard ceiling — always fires (any sensitivity except OFF).
        if threshold and value >= float(threshold):
            return {
                "metric_type": metric_type,
                "metric_name": metric_name,
                "metric_value": value,
                "anomaly_score": 1.0,
                "severity": self._calculate_severity(value, float(threshold)),
                "explanation": f"{label} reached {value:.1f}%, hitting the alert limit of {float(threshold):.0f}%.",
            }

        # 2) Robust baseline — upward deviation only.
        vals = [float(v) for v in history if v is not None]
        if len(vals) < self.MIN_BASELINE_POINTS:
            return None
        arr = np.array(vals, dtype=float)
        median = float(np.median(arr))
        mad = float(np.median(np.abs(arr - median)))
        if mad >= 1e-6:
            z = 0.6745 * (value - median) / mad
            normal_hi = median + (k / 0.6745) * mad
        else:
            std = float(np.std(arr))
            if std < 1e-6:
                return None  # perfectly flat history and below ceiling → not anomalous
            z = (value - median) / std
            normal_hi = median + k * std

        if z < k:
            return None
        # Absolute-deviation floor: ignore statistically-large but practically-trivial
        # wiggles on very flat baselines (e.g. CPU 4% on an idle box is NOT an incident).
        if (value - median) < self.MIN_ABS_DELTA:
            return None
        # Absolute-value floor: only an incident if the value is also genuinely high
        # (a 25% CPU blip on an idle box is normal background activity, not an anomaly).
        floor = self.ABS_VALUE_FLOOR.get(metric_type, 50.0)
        if value < floor:
            return None
        # Sustained gate: the elevation must persist across recent samples, so a single
        # transient spike doesn't fire (`vals` is the trailing history, most-recent first).
        recent = [value] + vals[: self.SUSTAIN_SAMPLES - 1]
        if len(recent) < self.SUSTAIN_SAMPLES or any(v < floor for v in recent):
            return None

        # Severity from the actual level relative to the ceiling (impact-based, not σ).
        # A baseline deviation stays below CRITICAL — only a ceiling crossing is CRITICAL.
        if threshold:
            ratio = value / float(threshold)
            if ratio >= 0.75:
                severity = Anomaly.Severity.HIGH
            elif ratio >= 0.5:
                severity = Anomaly.Severity.MEDIUM
            else:
                severity = Anomaly.Severity.LOW
        else:
            severity = Anomaly.Severity.MEDIUM

        return {
            "metric_type": metric_type,
            "metric_name": metric_name,
            "metric_value": value,
            "anomaly_score": min(z / 6.0, 1.0),
            "severity": severity,
            "explanation": (
                f"{label} rose to {value:.1f}%, well above its usual ~{median:.1f}% "
                f"(normally under ~{normal_hi:.0f}%)."
            ),
        }

    def _disk_percents(self, metric):
        """{mount: percent} parsed from a metric's disk_usage (dict or JSON string)."""
        out = {}
        data = getattr(metric, "disk_usage", None)
        if not data:
            return out
        try:
            if isinstance(data, str):
                data = json.loads(data)
            for mount, usage in data.items():
                if is_ephemeral_mount(mount):
                    continue  # /tmp, /var/tmp, /run, ... are not capacity incidents
                if isinstance(usage, dict):
                    out[mount] = float(usage.get("percent", 0) or 0)
                elif isinstance(usage, (int, float)):
                    out[mount] = float(usage)
        except Exception as e:
            logger.warning(f"disk_usage parse failed for {self.server.name}: {e}")
        return out

    def _network_ceiling(self, metric):
        """Ceiling-only network throughput check (MB/s vs config.network_io_threshold)."""
        if not getattr(metric, "network_io", None):
            return None
        threshold = float(getattr(self.config, "network_io_threshold", 0) or 0)
        if threshold <= 0:
            return None
        try:
            previous = (
                SystemMetric.objects.filter(server=self.server, timestamp__lt=metric.timestamp)
                .order_by("-timestamp")
                .first()
            )
            if not previous or not previous.network_io:
                return None
            cur, prev = metric.network_io, previous.network_io
            if isinstance(cur, str):
                cur = json.loads(cur)
            if isinstance(prev, str):
                prev = json.loads(prev)
            dt = (metric.timestamp - previous.timestamp).total_seconds() or 60
            worst = None
            for iface, c in cur.items():
                if iface == "lo" or iface not in prev:
                    continue
                p = prev[iface]
                if not (isinstance(c, dict) and isinstance(p, dict)):
                    continue
                delta = (c.get("bytes_sent", 0) - p.get("bytes_sent", 0)) + \
                        (c.get("bytes_recv", 0) - p.get("bytes_recv", 0))
                mbps = (delta / (1024 * 1024)) / dt if dt > 0 else 0  # MB/s
                if mbps > threshold and (worst is None or mbps > worst[1]):
                    worst = (iface, mbps)
            if not worst:
                return None
            iface, mbps = worst
            return {
                "metric_type": "network",
                "metric_name": f"network_throughput_{iface}",
                "metric_value": mbps,
                "anomaly_score": min(mbps / (threshold * 5.0), 1.0),
                "severity": self._calculate_severity(mbps, threshold),
                "explanation": f"Network {iface} throughput {mbps:.1f} MB/s exceeded the ceiling of {threshold:.0f} MB/s.",
            }
        except Exception as e:
            logger.warning(f"Network ceiling check failed for {self.server.name}: {e}")
            return None


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
                    # Check if current metric exceeds threshold directly
                    if metric.cpu_percent and metric.cpu_percent > self.config.cpu_threshold:
                        anomalies.append({
                            "metric_type": "cpu",
                            "metric_name": "cpu_percent",
                            "metric_value": metric.cpu_percent,
                            "anomaly_score": 1.0,
                            "severity": self._calculate_severity(metric.cpu_percent, self.config.cpu_threshold),
                        })
                    else:
                        # Also try ADTK ThresholdAD for pattern-based detection
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
                    # Check if current metric exceeds threshold directly
                    if metric.memory_percent and metric.memory_percent > self.config.memory_threshold:
                        anomalies.append({
                            "metric_type": "memory",
                            "metric_name": "memory_percent",
                            "metric_value": metric.memory_percent,
                            "anomaly_score": 1.0,
                            "severity": self._calculate_severity(metric.memory_percent, self.config.memory_threshold),
                        })
                    else:
                        # Also try ADTK ThresholdAD for pattern-based detection
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
            # IMPORTANT: psutil.net_io_counters() returns CUMULATIVE counters (total since boot)
            # We need to calculate the delta between current and previous metric
            if hasattr(metric, 'network_io') and metric.network_io:
                try:
                    # Get previous metric to calculate delta
                    previous_metric = SystemMetric.objects.filter(
                        server=self.server
                    ).order_by("-timestamp").exclude(id=metric.id).first()
                    
                    if not previous_metric or not previous_metric.network_io:
                        # Skip if no previous metric for delta calculation
                        logger.debug(f"No previous metric for network delta calculation on {self.server.name}")
                    else:
                        # Parse current network_io
                        if isinstance(metric.network_io, str):
                            current_network_data = json.loads(metric.network_io)
                        else:
                            current_network_data = metric.network_io
                        
                        # Parse previous network_io
                        if isinstance(previous_metric.network_io, str):
                            previous_network_data = json.loads(previous_metric.network_io)
                        else:
                            previous_network_data = previous_metric.network_io
                        
                        # Calculate time difference
                        time_diff_seconds = (metric.timestamp - previous_metric.timestamp).total_seconds()
                        if time_diff_seconds <= 0:
                            time_diff_seconds = 60  # Default to 60 seconds if invalid
                        
                        # Calculate delta for each interface
                        for interface, current_io in current_network_data.items():
                            if interface not in previous_network_data:
                                continue
                            
                            if isinstance(current_io, dict) and isinstance(previous_network_data[interface], dict):
                                # Calculate bytes transferred in this interval
                                bytes_sent_delta = current_io.get("bytes_sent", 0) - previous_network_data[interface].get("bytes_sent", 0)
                                bytes_recv_delta = current_io.get("bytes_recv", 0) - previous_network_data[interface].get("bytes_recv", 0)
                                total_bytes_delta = bytes_sent_delta + bytes_recv_delta
                                
                                # Skip loopback interface (lo) - it's always high
                                if interface == "lo":
                                    continue
                                
                                # Calculate throughput rate (Mbps)
                                throughput_mbps = (total_bytes_delta * 8) / (time_diff_seconds * 1024 * 1024) if time_diff_seconds > 0 else 0
                                
                                # Threshold: flag if throughput > 100 Mbps (adjustable)
                                # This is a reasonable threshold for most servers
                                threshold_mbps = 100.0
                                
                                if throughput_mbps > threshold_mbps:
                                    # Calculate severity based on throughput
                                    excess_ratio = throughput_mbps / threshold_mbps
                                    if excess_ratio > 5.0:
                                        severity = Anomaly.Severity.CRITICAL
                                    elif excess_ratio > 2.0:
                                        severity = Anomaly.Severity.HIGH
                                    elif excess_ratio > 1.5:
                                        severity = Anomaly.Severity.MEDIUM
                                    else:
                                        severity = Anomaly.Severity.LOW
                                    
                                    anomalies.append({
                                        "metric_type": "network",
                                        "metric_name": f"network_throughput_{interface}",
                                        "metric_value": throughput_mbps,  # Store as Mbps
                                        "anomaly_score": min(excess_ratio / 5.0, 1.0),  # Normalize to 0-1
                                        "severity": severity,
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
