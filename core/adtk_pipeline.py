"""
ADTK Pipeline Subsystem

This module provides a unified, reusable ADTK-based anomaly detection pipeline
that can be integrated into the existing StackSense anomaly detection system.

The pipeline handles:
- Time series preprocessing and normalization
- Configurable detector management
- Unified anomaly detection results
- Extensible architecture for future improvements

Author: StackSense Development Team
"""

import numpy as np
import pandas as pd
from typing import List, Dict, Any, Optional, Tuple, Union
from datetime import datetime, timedelta
from django.utils import timezone

try:
    from adtk.detector import (
        ThresholdAD,
        PersistAD,
        LevelShiftAD,
        VolatilityShiftAD
    )
    ADTK_AVAILABLE = True
except ImportError:
    ADTK_AVAILABLE = False
    # Create dummy classes for type hints when ADTK is not available
    class ThresholdAD:
        pass
    class PersistAD:
        pass
    class LevelShiftAD:
        pass
    class VolatilityShiftAD:
        pass


def prepare_series(
    values: List[float],
    timestamps: List[datetime],
    freq: str = "1min"
) -> pd.Series:
    """
    Prepare and clean a time series for ADTK analysis.
    
    This function converts raw metric data into a properly formatted pandas Series
    with regular frequency intervals, handling missing values and edge cases.
    
    Args:
        values: List of numeric metric values (e.g., CPU percentages)
        timestamps: List of datetime objects corresponding to each value
        freq: Pandas frequency string for resampling (default: "1min")
             Examples: "1min", "30s", "5min", "1H"
    
    Returns:
        pd.Series: Cleaned time series with DatetimeIndex and regular frequency
        
    Raises:
        ValueError: If values and timestamps have different lengths
        ValueError: If values list is empty
        TypeError: If values contain non-numeric data
    
    Example:
        >>> values = [45.2, 47.8, 46.1]
        >>> timestamps = [datetime.now() - timedelta(minutes=2),
        ...               datetime.now() - timedelta(minutes=1),
        ...               datetime.now()]
        >>> series = prepare_series(values, timestamps, freq="1min")
        >>> print(series.index.freq)
        <Minute>
    """
    if len(values) != len(timestamps):
        raise ValueError(
            f"Values and timestamps must have the same length. "
            f"Got {len(values)} values and {len(timestamps)} timestamps."
        )
    
    if len(values) == 0:
        raise ValueError("Cannot prepare series from empty data.")
    
    # Convert to numpy array for validation
    try:
        values_array = np.array(values, dtype=np.float64)
    except (ValueError, TypeError) as e:
        raise TypeError(
            f"All values must be numeric. Error: {e}"
        )
    
    # Check for NaN or infinite values
    if np.any(np.isnan(values_array)) or np.any(np.isinf(values_array)):
        # Replace NaN and Inf with None for proper handling
        values_array = np.where(
            np.isnan(values_array) | np.isinf(values_array),
            None,
            values_array
        )
    
    # Create initial Series with DatetimeIndex
    # Ensure timestamps are timezone-aware if Django timezone is used
    if timestamps and hasattr(timestamps[0], 'tzinfo') and timestamps[0].tzinfo is None:
        # If timezone-naive, assume UTC or use Django's timezone
        try:
            timestamps = [timezone.make_aware(ts) if timezone.is_naive(ts) else ts 
                         for ts in timestamps]
        except:
            pass  # If timezone conversion fails, proceed with original timestamps
    
    series = pd.Series(
        values_array,
        index=pd.DatetimeIndex(timestamps),
        dtype='float64'
    )
    
    # Remove duplicate timestamps (keep last value)
    if series.index.duplicated().any():
        series = series[~series.index.duplicated(keep='last')]
        series = series.sort_index()
    
    # Resample to regular frequency
    # This handles irregular intervals by creating a regular grid
    try:
        series_resampled = series.resample(freq).mean()
    except Exception as e:
        # If resampling fails, try with nearest method
        series_resampled = series.resample(freq).nearest()
    
    # Forward fill missing values (carry last known value forward)
    series_resampled = series_resampled.fillna(method='ffill')
    
    # Backward fill any remaining NaN values at the beginning
    series_resampled = series_resampled.fillna(method='bfill')
    
    # If still NaN values exist, fill with 0 or mean (last resort)
    if series_resampled.isna().any():
        fill_value = series_resampled.mean() if not series_resampled.isna().all() else 0.0
        series_resampled = series_resampled.fillna(fill_value)
    
    # Validate final dtype
    if not pd.api.types.is_numeric_dtype(series_resampled):
        series_resampled = pd.to_numeric(series_resampled, errors='coerce')
        series_resampled = series_resampled.fillna(0.0)
    
    # Ensure index has frequency information
    if series_resampled.index.freq is None:
        series_resampled.index.freq = pd.tseries.frequencies.to_offset(freq)
    
    return series_resampled


class ADTKDetectorFactory:
    """
    Factory class for creating configurable ADTK detector instances.
    
    This factory encapsulates detector creation logic and provides a consistent
    interface for building detectors with parameters from MonitoringConfig.
    
    All detector parameters are configurable and can be overridden with defaults
    if not specified in the configuration.
    """
    
    @staticmethod
    def threshold(
        high: float,
        low: float = 0.0,
        **kwargs
    ) -> ThresholdAD:
        """
        Create a ThresholdAD detector for detecting values outside a range.
        
        Args:
            high: Upper threshold value. Values above this are considered anomalies.
            low: Lower threshold value. Values below this are considered anomalies.
                 Default: 0.0
            **kwargs: Additional parameters passed to ThresholdAD constructor
        
        Returns:
            ThresholdAD: Configured threshold anomaly detector
            
        Example:
            >>> factory = ADTKDetectorFactory()
            >>> detector = factory.threshold(high=80.0, low=10.0)
        """
        if not ADTK_AVAILABLE:
            raise ImportError(
                "ADTK is not available. Please install adtk package: pip install adtk"
            )
        
        return ThresholdAD(high=high, low=low, **kwargs)
    
    @staticmethod
    def persist(
        window: int = 5,
        c: float = 3.0,
        **kwargs
    ) -> PersistAD:
        """
        Create a PersistAD detector for detecting persistent anomalies.
        
        PersistAD detects anomalies that persist for a certain duration,
        useful for detecting sustained high/low values.
        
        Args:
            window: Window size for persistence check. Default: 5
            c: Sensitivity parameter. Higher values = less sensitive. Default: 3.0
            **kwargs: Additional parameters passed to PersistAD constructor
        
        Returns:
            PersistAD: Configured persistence anomaly detector
            
        Example:
            >>> factory = ADTKDetectorFactory()
            >>> detector = factory.persist(window=10, c=2.5)
        """
        if not ADTK_AVAILABLE:
            raise ImportError(
                "ADTK is not available. Please install adtk package: pip install adtk"
            )
        
        return PersistAD(window=window, c=c, **kwargs)
    
    @staticmethod
    def levelshift(
        window: int = 10,
        threshold: float = 3.0,
        **kwargs
    ) -> LevelShiftAD:
        """
        Create a LevelShiftAD detector for detecting level shifts in time series.
        
        LevelShiftAD detects sudden changes in the baseline level of a metric,
        useful for detecting system state changes.
        
        Args:
            window: Window size for level shift detection. Default: 10
            threshold: Threshold for level shift magnitude. Default: 3.0
            **kwargs: Additional parameters passed to LevelShiftAD constructor
        
        Returns:
            LevelShiftAD: Configured level shift anomaly detector
            
        Example:
            >>> factory = ADTKDetectorFactory()
            >>> detector = factory.levelshift(window=15, threshold=2.5)
        """
        if not ADTK_AVAILABLE:
            raise ImportError(
                "ADTK is not available. Please install adtk package: pip install adtk"
            )
        
        return LevelShiftAD(window=window, threshold=threshold, **kwargs)
    
    @staticmethod
    def volatility(
        window: int = 10,
        c: float = 3.0,
        **kwargs
    ) -> VolatilityShiftAD:
        """
        Create a VolatilityShiftAD detector for detecting volatility changes.
        
        VolatilityShiftAD detects changes in the volatility (variance) of a metric,
        useful for detecting changes in system stability.
        
        Args:
            window: Window size for volatility detection. Default: 10
            c: Sensitivity parameter. Higher values = less sensitive. Default: 3.0
            **kwargs: Additional parameters passed to VolatilityShiftAD constructor
        
        Returns:
            VolatilityShiftAD: Configured volatility shift anomaly detector
            
        Example:
            >>> factory = ADTKDetectorFactory()
            >>> detector = factory.volatility(window=20, c=2.0)
        """
        if not ADTK_AVAILABLE:
            raise ImportError(
                "ADTK is not available. Please install adtk package: pip install adtk"
            )
        
        return VolatilityShiftAD(window=window, c=c, **kwargs)


class ADTKPipeline:
    """
    Unified ADTK-based anomaly detection pipeline.
    
    This class provides a clean, reusable interface for anomaly detection using
    ADTK detectors. It handles preprocessing, detector management, and result
    aggregation.
    
    The pipeline is designed to be:
    - Configurable through MonitoringConfig
    - Extensible for future detector additions
    - Independent of existing anomaly_detector.py (can be integrated later)
    - Production-ready with proper error handling
    
    Attributes:
        server: Server instance being monitored
        config: MonitoringConfig instance with detection parameters
        detector_registry: Dictionary storing detector instances (lazy-loaded)
        factory: ADTKDetectorFactory instance for creating detectors
        preprocessing_freq: Frequency string for time series resampling
    
    Example:
        >>> from core.models import Server, MonitoringConfig
        >>> server = Server.objects.get(id=1)
        >>> config = server.monitoring_config
        >>> pipeline = ADTKPipeline(server, config)
        >>> 
        >>> # Prepare data
        >>> values = [45.2, 47.8, 46.1, 85.3, 87.2]
        >>> timestamps = [datetime.now() - timedelta(minutes=i) 
        ...               for i in range(5, 0, -1)]
        >>> 
        >>> # Run detection
        >>> detectors = ['threshold', 'persist']
        >>> result = pipeline.detect(values, timestamps, detectors)
        >>> print(result['is_anomaly'])
        True
    """
    
    def __init__(self, server, config):
        """
        Initialize the ADTK pipeline with server and configuration.
        
        Args:
            server: Server model instance
            config: MonitoringConfig model instance
        
        Raises:
            ValueError: If config is None or invalid
            ImportError: If ADTK is not available and use_adtk is True
        """
        if config is None:
            raise ValueError("MonitoringConfig cannot be None")
        
        if not ADTK_AVAILABLE and getattr(config, 'use_adtk', True):
            raise ImportError(
                "ADTK is required but not available. "
                "Install with: pip install adtk"
            )
        
        self.server = server
        self.config = config
        self.detector_registry = {}
        self.factory = ADTKDetectorFactory()
        
        # Load configurable parameters from MonitoringConfig
        self.threshold_factor = getattr(config, 'adtk_threshold_factor', 2.0)
        self.adtk_window_size = getattr(config, 'adtk_window_size', 30)
        self.detection_interval = getattr(config, 'anomaly_detection_interval', 15)
        self.contamination = getattr(config, 'contamination', 0.1)
        self.adaptive_collection_enabled = getattr(
            config, 'adaptive_collection_enabled', False
        )
        self.use_adtk = getattr(config, 'use_adtk', True)
        
        # Preprocessing configuration
        # Default to 1 minute frequency, but can be adjusted based on collection interval
        collection_interval = getattr(config, 'collection_interval_seconds', 60)
        if collection_interval < 60:
            self.preprocessing_freq = f"{collection_interval}s"
        elif collection_interval == 60:
            self.preprocessing_freq = "1min"
        else:
            # For longer intervals, use the collection interval as frequency
            minutes = collection_interval // 60
            self.preprocessing_freq = f"{minutes}min"
    
    def preprocess(
        self,
        values: List[float],
        timestamps: List[datetime]
    ) -> pd.Series:
        """
        Preprocess raw metric data into a clean time series.
        
        This method applies all preprocessing steps:
        - Conversion to pandas Series with DatetimeIndex
        - Resampling to stable frequency
        - Interpolation of missing values
        - Forward-fill and back-fill edge cases
        - Data type validation
        
        Args:
            values: List of numeric metric values
            timestamps: List of datetime objects corresponding to values
        
        Returns:
            pd.Series: Preprocessed time series ready for ADTK analysis
            
        Raises:
            ValueError: If preprocessing fails due to invalid data
        """
        try:
            series = prepare_series(
                values,
                timestamps,
                freq=self.preprocessing_freq
            )
            return series
        except Exception as e:
            raise ValueError(
                f"Preprocessing failed for server {self.server.name}: {e}"
            )
    
    def get_threshold_detector(
        self,
        metric_name: str,
        high_threshold: Optional[float] = None
    ) -> ThresholdAD:
        """
        Get or create a ThresholdAD detector for a specific metric.
        
        Detectors are cached in the registry to avoid recreation.
        Threshold values are calculated from MonitoringConfig thresholds
        with optional threshold_factor multiplier.
        
        Args:
            metric_name: Name of the metric (e.g., "cpu", "memory", "disk")
            high_threshold: Optional override for high threshold.
                          If None, uses config threshold * threshold_factor
        
        Returns:
            ThresholdAD: Configured threshold detector
            
        Example:
            >>> detector = pipeline.get_threshold_detector("cpu")
            >>> # Uses config.cpu_threshold * threshold_factor
        """
        cache_key = f"threshold_{metric_name}"
        
        if cache_key in self.detector_registry:
            return self.detector_registry[cache_key]
        
        # Determine threshold based on metric type
        if high_threshold is None:
            if metric_name.lower() in ['cpu', 'cpu_percent']:
                base_threshold = getattr(self.config, 'cpu_threshold', 80.0)
            elif metric_name.lower() in ['memory', 'memory_percent', 'ram']:
                base_threshold = getattr(self.config, 'memory_threshold', 90.0)
            elif metric_name.lower().startswith('disk'):
                base_threshold = getattr(self.config, 'disk_threshold', 90.0)
            else:
                # Default threshold for unknown metrics
                base_threshold = 80.0
            
            high_threshold = base_threshold * self.threshold_factor
        
        detector = self.factory.threshold(high=high_threshold, low=0.0)
        self.detector_registry[cache_key] = detector
        
        return detector
    
    def get_persist_detector(
        self,
        window: Optional[int] = None,
        c: Optional[float] = None
    ) -> PersistAD:
        """
        Get or create a PersistAD detector.
        
        Args:
            window: Window size for persistence check.
                   If None, uses a default based on adtk_window_size
            c: Sensitivity parameter. If None, uses default 3.0
        
        Returns:
            PersistAD: Configured persistence detector
        """
        cache_key = "persist"
        
        if cache_key in self.detector_registry:
            return self.detector_registry[cache_key]
        
        # Use defaults if not specified
        if window is None:
            # Use a fraction of window size for persistence window
            window = max(5, self.adtk_window_size // 6)
        
        if c is None:
            c = 3.0
        
        detector = self.factory.persist(window=window, c=c)
        self.detector_registry[cache_key] = detector
        
        return detector
    
    def get_levelshift_detector(
        self,
        window: Optional[int] = None,
        threshold: Optional[float] = None
    ) -> LevelShiftAD:
        """
        Get or create a LevelShiftAD detector.
        
        Args:
            window: Window size for level shift detection.
                   If None, uses a default based on adtk_window_size
            threshold: Threshold for level shift magnitude.
                      If None, uses default 3.0
        
        Returns:
            LevelShiftAD: Configured level shift detector
        """
        cache_key = "levelshift"
        
        if cache_key in self.detector_registry:
            return self.detector_registry[cache_key]
        
        if window is None:
            window = max(10, self.adtk_window_size // 3)
        
        if threshold is None:
            threshold = 3.0
        
        detector = self.factory.levelshift(window=window, threshold=threshold)
        self.detector_registry[cache_key] = detector
        
        return detector
    
    def get_volatilityshift_detector(
        self,
        window: Optional[int] = None,
        c: Optional[float] = None
    ) -> VolatilityShiftAD:
        """
        Get or create a VolatilityShiftAD detector.
        
        Args:
            window: Window size for volatility detection.
                   If None, uses a default based on adtk_window_size
            c: Sensitivity parameter. If None, uses default 3.0
        
        Returns:
            VolatilityShiftAD: Configured volatility shift detector
        """
        cache_key = "volatility"
        
        if cache_key in self.detector_registry:
            return self.detector_registry[cache_key]
        
        if window is None:
            window = max(10, self.adtk_window_size // 3)
        
        if c is None:
            c = 3.0
        
        detector = self.factory.volatility(window=window, c=c)
        self.detector_registry[cache_key] = detector
        
        return detector
    
    def detect(
        self,
        values: List[float],
        timestamps: List[datetime],
        detector_list: List[str],
        metric_name: str = "unknown"
    ) -> Dict[str, Any]:
        """
        Run the complete anomaly detection pipeline.
        
        This method:
        1. Preprocesses the input data
        2. Runs each specified detector
        3. Merges anomaly points from all detectors
        4. Returns unified results
        
        Args:
            values: List of numeric metric values
            timestamps: List of datetime objects
            detector_list: List of detector names to use.
                          Valid names: 'threshold', 'persist', 'levelshift', 'volatility'
            metric_name: Name of the metric being analyzed (for threshold detector)
        
        Returns:
            Dictionary with detection results:
            {
                "is_anomaly": bool,           # True if any detector found anomaly
                "indices": List[int],          # Indices of anomalous points
                "timestamps": List[datetime],  # Timestamps of anomalous points
                "scores": Dict[str, float],    # Scores from each detector
                "detector_flags": Dict[str, bool],  # Which detectors flagged anomalies
                "latest_anomaly": bool,        # True if latest point is anomalous
                "series": pd.Series            # Preprocessed series (for debugging)
            }
        
        Raises:
            ValueError: If detector_list is empty or contains invalid detector names
            ImportError: If ADTK is not available
        
        Example:
            >>> result = pipeline.detect(
            ...     values=[45, 46, 47, 85, 87],
            ...     timestamps=[...],
            ...     detector_list=['threshold', 'persist'],
            ...     metric_name='cpu'
            ... )
            >>> if result['is_anomaly']:
            ...     print(f"Anomaly detected at indices: {result['indices']}")
        """
        if not ADTK_AVAILABLE:
            raise ImportError(
                "ADTK is not available. Cannot run detection pipeline."
            )
        
        if not detector_list:
            raise ValueError("detector_list cannot be empty")
        
        # Validate detector names
        valid_detectors = ['threshold', 'persist', 'levelshift', 'volatility']
        invalid = [d for d in detector_list if d not in valid_detectors]
        if invalid:
            raise ValueError(
                f"Invalid detector names: {invalid}. "
                f"Valid names: {valid_detectors}"
            )
        
        # Preprocess the data
        try:
            series = self.preprocess(values, timestamps)
        except Exception as e:
            return {
                "is_anomaly": False,
                "indices": [],
                "timestamps": [],
                "scores": {},
                "detector_flags": {},
                "latest_anomaly": False,
                "series": None,
                "error": str(e)
            }
        
        # Run each detector and collect results
        all_anomaly_indices = set()
        detector_flags = {}
        scores = {}
        
        for detector_name in detector_list:
            try:
                if detector_name == 'threshold':
                    detector = self.get_threshold_detector(metric_name)
                    anomaly_series = detector.detect(series)
                elif detector_name == 'persist':
                    detector = self.get_persist_detector()
                    anomaly_series = detector.detect(series)
                elif detector_name == 'levelshift':
                    detector = self.get_levelshift_detector()
                    anomaly_series = detector.detect(series)
                elif detector_name == 'volatility':
                    detector = self.get_volatilityshift_detector()
                    anomaly_series = detector.detect(series)
                else:
                    continue
                
                # Extract anomaly indices
                if not anomaly_series.empty:
                    anomaly_mask = anomaly_series.fillna(False)
                    if anomaly_mask.dtype == bool:
                        anomaly_indices = series.index[anomaly_mask].tolist()
                    else:
                        # If not boolean, convert to boolean
                        anomaly_mask = anomaly_mask.astype(bool)
                        anomaly_indices = series.index[anomaly_mask].tolist()
                    
                    # Convert to integer indices
                    for ts in anomaly_indices:
                        idx = series.index.get_loc(ts)
                        all_anomaly_indices.add(idx)
                    
                    # Check if latest point is anomalous
                    if len(series) > 0:
                        latest_idx = len(series) - 1
                        latest_anomalous = latest_idx in all_anomaly_indices
                    else:
                        latest_anomalous = False
                    
                    detector_flags[detector_name] = len(anomaly_indices) > 0
                    
                    # Calculate a simple score (proportion of anomalies)
                    if len(series) > 0:
                        score = len(anomaly_indices) / len(series)
                    else:
                        score = 0.0
                    scores[detector_name] = score
                else:
                    detector_flags[detector_name] = False
                    scores[detector_name] = 0.0
                    
            except Exception as e:
                # If a detector fails, log but continue with others
                detector_flags[detector_name] = False
                scores[detector_name] = 0.0
                continue
        
        # Convert set to sorted list
        anomaly_indices = sorted(list(all_anomaly_indices))
        
        # Get timestamps for anomalous points
        anomaly_timestamps = [
            series.index[i] for i in anomaly_indices
        ] if anomaly_indices else []
        
        # Check if latest point is anomalous
        latest_anomaly = (
            len(series) > 0 and
            (len(series) - 1) in all_anomaly_indices
        )
        
        return {
            "is_anomaly": len(all_anomaly_indices) > 0,
            "indices": anomaly_indices,
            "timestamps": anomaly_timestamps,
            "scores": scores,
            "detector_flags": detector_flags,
            "latest_anomaly": latest_anomaly,
            "series": series
        }

