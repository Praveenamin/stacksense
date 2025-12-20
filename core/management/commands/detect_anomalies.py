from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from core.models import SystemMetric, Anomaly, MonitoringConfig, EmailAlertConfig
from core.views import _send_alert_email
from core.anomaly_detector import AnomalyDetector
from core.llm_analyzer import OllamaAnalyzer
from core.utils import collect_processes_on_demand


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
                
                # Collect anomalies for this metric to send in one email
                anomaly_alerts = []
                
                for anomaly_data in detected:
                    # Deduplication: Check if there's already an unresolved anomaly of the same type
                    # within the last 10 minutes to avoid creating multiple anomalies for the same spike
                    recent_window = timezone.now() - timedelta(minutes=10)
                    existing_anomaly = Anomaly.objects.filter(
                        server=metric.server,
                        metric_type=anomaly_data['metric_type'],
                        metric_name=anomaly_data['metric_name'],
                        resolved=False,
                        timestamp__gte=recent_window
                    ).order_by('-timestamp').first()
                    
                    if existing_anomaly:
                        # Skip creating duplicate anomaly - one already exists for this spike
                        self.stdout.write(
                            self.style.WARNING(
                                f"⚠ Skipping duplicate anomaly: {metric.server.name} - "
                                f"{anomaly_data['metric_type']} {anomaly_data['metric_name']} "
                                f"(existing anomaly ID: {existing_anomaly.id})"
                            )
                        )
                        continue
                    
                    anomaly = Anomaly.objects.create(
                        server=metric.server,
                        metric=metric,
                        **anomaly_data
                    )
                    anomaly_count += 1
                    
                    # Add to alerts list for email notification
                    if anomaly_data.get('severity') in ['HIGH', 'CRITICAL']:
                        anomaly_alerts.append({
                            'type': 'Anomaly',
                            'value': anomaly_data['metric_value'],
                            'threshold': None,
                            'message': f"Anomaly detected: {anomaly_data['metric_type']} {anomaly_data['metric_name']} = {anomaly_data['metric_value']:.2f} (severity: {anomaly_data['severity']})"
                        })
                    
                    # Always generate LLM explanation if enabled
                    if config.use_llm_explanation and llm_analyzer:
                        try:
                            # Multi-tier process context collection
                            process_context = None
                            
                            # Tier 1: Use pre-collected process data from metric
                            if metric.top_processes:
                                process_context = metric.top_processes
                                metric_type = anomaly_data["metric_type"]
                                
                                # Extract relevant processes for this anomaly type
                                relevant_processes = []
                                if metric_type == 'cpu' and process_context.get('cpu'):
                                    relevant_processes = process_context.get('cpu', [])
                                elif metric_type == 'memory' and process_context.get('memory'):
                                    relevant_processes = process_context.get('memory', [])
                                
                                # Build process context with only relevant processes
                                if relevant_processes:
                                    process_context = {
                                        'cpu': relevant_processes if metric_type == 'cpu' else [],
                                        'memory': relevant_processes if metric_type == 'memory' else []
                                    }
                                else:
                                    process_context = None  # No relevant processes found
                            
                            # Tier 2: On-demand collection (fallback for HIGH/CRITICAL if data missing)
                            if not process_context and anomaly_data.get('severity') in ['HIGH', 'CRITICAL']:
                                try:
                                    on_demand_data = collect_processes_on_demand(
                                        server=metric.server,
                                        metric_type=anomaly_data["metric_type"],
                                        timeout=5  # Short timeout to avoid blocking
                                    )
                                    if on_demand_data:
                                        process_context = on_demand_data
                                except Exception as e:
                                    # Log but don't fail - fall back to generic explanation
                                    self.stdout.write(self.style.WARNING(f"On-demand process collection failed: {e}"))
                            
                            # Generate explanation with or without process context
                            explanation = llm_analyzer.explain_anomaly(
                                metric_type=anomaly_data["metric_type"],
                                metric_name=anomaly_data["metric_name"],
                                metric_value=anomaly_data["metric_value"],
                                server_name=metric.server.name,
                                process_context=process_context  # Can be None
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
                
                # Send email alert for HIGH/CRITICAL anomalies
                if anomaly_alerts:
                    try:
                        email_config = EmailAlertConfig.objects.filter(enabled=True).first()
                        if email_config:
                            # Refresh server to check if alerts are suppressed
                            metric.server.refresh_from_db()
                            server_config = metric.server.monitoring_config
                            if (not server_config.monitoring_suspended and 
                                not server_config.alert_suppressed):
                                _send_alert_email(email_config, metric.server, anomaly_alerts)
                                self.stdout.write(
                                    self.style.SUCCESS(
                                        f"✓ Sent anomaly alert email for {metric.server.name}"
                                    )
                                )
                    except Exception as e:
                        self.stdout.write(self.style.WARNING(f"Failed to send anomaly alert email: {e}"))
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"Error detecting anomalies for {metric.server.name}: {e}"))
        
        if anomaly_count > 0:
            self.stdout.write(self.style.SUCCESS(f"✓ Detected {anomaly_count} anomaly/anomalies"))
        else:
            self.stdout.write("✓ No anomalies detected")
