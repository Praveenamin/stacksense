from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from core.models import SystemMetric, Server, MonitoringConfig


class Command(BaseCommand):
    help = "Deletes old raw metrics based on retention period"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be deleted without actually deleting"
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        
        servers = Server.objects.filter(monitoring_config__aggregation_enabled=True)
        
        if not servers.exists():
            self.stdout.write(self.style.WARNING("No servers with aggregation enabled."))
            return
        
        total_deleted = 0
        
        for server in servers:
            try:
                config = server.monitoring_config
                retention_days = config.retention_period_days
                cutoff_date = timezone.now() - timedelta(days=retention_days)
                
                old_metrics = SystemMetric.objects.filter(
                    server=server,
                    timestamp__lt=cutoff_date
                )
                
                count = old_metrics.count()
                
                if count > 0:
                    if dry_run:
                        self.stdout.write(
                            f"Would delete {count} metrics from {server.name} "
                            f"(older than {retention_days} days)"
                        )
                    else:
                        old_metrics.delete()
                        self.stdout.write(
                            self.style.SUCCESS(
                                f"✓ Deleted {count} old metrics from {server.name}"
                            )
                        )
                        total_deleted += count
                else:
                    self.stdout.write(f"No old metrics to delete for {server.name}")
                    
            except Exception as e:
                self.stderr.write(
                    self.style.ERROR(f"Error processing {server.name}: {str(e)}")
                )
        
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN - No metrics were actually deleted"))
        elif total_deleted > 0:
            self.stdout.write(
                self.style.SUCCESS(f"✓ Total deleted: {total_deleted} metrics")
            )
        else:
            self.stdout.write("No metrics to delete")
