from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from core.models import SystemMetric, Anomaly, MonitoringConfig
from core.anomaly_detector import AnomalyDetector
from core.llm_analyzer import OllamaAnalyzer


class Command(BaseCommand):
    help = "Detects anomalies in collected metrics using ADTK/IsolationForest and generates LLM explanations"

    def handle(self, *args, **options):
        since = timezone.now() - timedelta(hours=1)
        
        latest_metrics = SystemMetric.objects.filter(
            timestamp__gte=since
        ).select_related("server", "server__monitoring_config").order_by("-timestamp")
        
        metrics_to_check = []
        for metric in latest_metrics:
            if not metric.anomalies.exists():
                metrics_to_check.append(metric)
        
        if not metrics_to_check:
            self.stdout.write("No new metrics to check for anomalies.")
            return
        
        self.stdout.write(f"Checking {len(metrics_to_check)} metrics for anomalies...")
        
        llm_analyzer = None
        try:
            llm_analyzer = OllamaAnalyzer()
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"LLM analyzer not available: {e}"))
        
        anomaly_count = 0
        for metric in metrics_to_check:
            config = getattr(metric.server, "monitoring_config", None)
            if not config or not config.enabled:
                continue
            
            try:
                detector = AnomalyDetector(metric.server, config)
                detected = detector.detect_anomalies(metric)
                
                for anomaly_data in detected:
                    anomaly = Anomaly.objects.create(
                        server=metric.server,
                        metric=metric,
                        **anomaly_data
                    )
                    anomaly_count += 1
                    
                    # Always generate LLM explanation if enabled
                    if config.use_llm_explanation and llm_analyzer:
                        try:
                            explanation = llm_analyzer.explain_anomaly(
                                anomaly_data["metric_type"],
                                anomaly_data["metric_name"],
                                anomaly_data["metric_value"],
                                metric.server.name
                            )
                            if explanation:
                                anomaly.explanation = explanation
                                anomaly.llm_generated = True
                                anomaly.save()
                        except Exception as e:
                            self.stdout.write(self.style.WARNING(f"Failed to generate LLM explanation: {e}"))
                    
                    self.stdout.write(
                        self.style.WARNING(
                            f"⚠ Anomaly detected: {metric.server.name} - "
                            f"{anomaly_data['metric_type']} {anomaly_data['metric_name']} = {anomaly_data['metric_value']:.2f} "
                            f"(severity: {anomaly_data['severity']})"
                        )
                    )
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"Error detecting anomalies for {metric.server.name}: {e}"))
        
        if anomaly_count > 0:
            self.stdout.write(self.style.SUCCESS(f"✓ Detected {anomaly_count} anomaly/anomalies"))
        else:
            self.stdout.write("✓ No anomalies detected")
