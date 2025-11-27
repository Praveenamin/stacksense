from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db.models import Avg, Min, Max, Count
from datetime import timedelta
from core.models import SystemMetric, AggregatedMetric, Server, MonitoringConfig


class Command(BaseCommand):
    help = "Aggregates old metrics into hourly and daily summaries"

    def add_arguments(self, parser):
        parser.add_argument(
            "--hours",
            type=int,
            default=24,
            help="Number of hours of data to aggregate (default: 24)"
        )

    def handle(self, *args, **options):
        hours = options["hours"]
        cutoff_time = timezone.now() - timedelta(hours=hours)
        
        servers = Server.objects.filter(monitoring_config__aggregation_enabled=True)
        
        if not servers.exists():
            self.stdout.write(self.style.WARNING("No servers with aggregation enabled."))
            return
        
        aggregated_count = 0
        
        for server in servers:
            # Hourly aggregation
            hourly_count = self._aggregate_hourly(server, cutoff_time)
            aggregated_count += hourly_count
            
            # Daily aggregation (for data older than 7 days)
            daily_cutoff = timezone.now() - timedelta(days=7)
            daily_count = self._aggregate_daily(server, daily_cutoff)
            aggregated_count += daily_count
        
        if aggregated_count > 0:
            self.stdout.write(self.style.SUCCESS(f"✓ Aggregated {aggregated_count} metric groups"))
        else:
            self.stdout.write("No metrics to aggregate")

    def _aggregate_hourly(self, server, cutoff_time):
        """Aggregate metrics into hourly summaries"""
        metrics = SystemMetric.objects.filter(
            server=server,
            timestamp__lt=cutoff_time
        ).order_by("timestamp")
        
        if not metrics.exists():
            return 0
        
        # Group by hour
        current_hour = None
        hour_metrics = []
        count = 0
        
        for metric in metrics:
            metric_hour = metric.timestamp.replace(minute=0, second=0, microsecond=0)
            
            if current_hour is None:
                current_hour = metric_hour
                hour_metrics = [metric]
            elif metric_hour == current_hour:
                hour_metrics.append(metric)
            else:
                # Aggregate this hour
                self._create_aggregated(server, "hourly", current_hour, hour_metrics)
                count += 1
                
                # Start new hour
                current_hour = metric_hour
                hour_metrics = [metric]
        
        # Aggregate last hour
        if hour_metrics:
            self._create_aggregated(server, "hourly", current_hour, hour_metrics)
            count += 1
        
        return count

    def _aggregate_daily(self, server, cutoff_time):
        """Aggregate metrics into daily summaries"""
        metrics = SystemMetric.objects.filter(
            server=server,
            timestamp__lt=cutoff_time
        ).order_by("timestamp")
        
        if not metrics.exists():
            return 0
        
        # Group by day
        current_day = None
        day_metrics = []
        count = 0
        
        for metric in metrics:
            metric_day = metric.timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
            
            if current_day is None:
                current_day = metric_day
                day_metrics = [metric]
            elif metric_day == current_day:
                day_metrics.append(metric)
            else:
                # Aggregate this day
                self._create_aggregated(server, "daily", current_day, day_metrics)
                count += 1
                
                # Start new day
                current_day = metric_day
                day_metrics = [metric]
        
        # Aggregate last day
        if day_metrics:
            self._create_aggregated(server, "daily", current_day, day_metrics)
            count += 1
        
        return count

    def _create_aggregated(self, server, agg_type, timestamp, metrics):
        """Create aggregated metric record"""
        cpu_values = [m.cpu_percent for m in metrics if m.cpu_percent is not None]
        memory_values = [m.memory_percent for m in metrics if m.memory_percent is not None]
        
        # Calculate disk averages
        disk_values = []
        for m in metrics:
            if m.disk_usage:
                for mount, usage in m.disk_usage.items():
                    if usage.get("percent"):
                        disk_values.append(usage["percent"])
        
        AggregatedMetric.objects.update_or_create(
            server=server,
            aggregation_type=agg_type,
            timestamp=timestamp,
            defaults={
                "cpu_avg": sum(cpu_values) / len(cpu_values) if cpu_values else None,
                "cpu_min": min(cpu_values) if cpu_values else None,
                "cpu_max": max(cpu_values) if cpu_values else None,
                "memory_avg": sum(memory_values) / len(memory_values) if memory_values else None,
                "memory_min": min(memory_values) if memory_values else None,
                "memory_max": max(memory_values) if memory_values else None,
                "disk_avg": sum(disk_values) / len(disk_values) if disk_values else None,
                "disk_min": min(disk_values) if disk_values else None,
                "disk_max": max(disk_values) if disk_values else None,
                "metric_count": len(metrics),
            }
        )
