"""
Django management command to calculate SLI values and compliance status.

This command calculates SLI values for all servers and enabled metric types,
compares them against SLO targets, and stores results in SLIMeasurement.

For RESPONSE_TIME metric: Only includes services with monitoring_enabled=True.

Usage:
    python manage.py calculate_sli_compliance [--time-window-days=7]
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from core.models import Server, SLIConfig, SLOConfig, SLIMeasurement
from core.sli_utils import (
    calculate_sli_value, get_slo_config, check_compliance
)


class Command(BaseCommand):
    help = "Calculate SLI values and compliance status for all servers"

    def add_arguments(self, parser):
        parser.add_argument(
            '--time-window-days',
            type=int,
            default=7,
            help='Time window in days for SLI calculation (default: 7)',
        )
        parser.add_argument(
            '--server-id',
            type=int,
            default=None,
            help='Calculate SLI for specific server ID only',
        )
        parser.add_argument(
            '--metric-type',
            type=str,
            default=None,
            help='Calculate SLI for specific metric type only',
        )

    def handle(self, *args, **options):
        time_window_days = options['time_window_days']
        server_id = options.get('server_id')
        metric_type = options.get('metric_type')
        
        now = timezone.now()
        start_date = now - timedelta(days=time_window_days)
        
        self.stdout.write(f"Calculating SLI compliance for time window: {start_date} to {now}")
        self.stdout.write("")
        
        # Get enabled SLI configs
        sli_configs = SLIConfig.objects.filter(enabled=True)
        if metric_type:
            sli_configs = sli_configs.filter(metric_type=metric_type)
        
        if not sli_configs.exists():
            self.stdout.write(self.style.WARNING("No enabled SLI configurations found."))
            return
        
        # Get servers
        servers = Server.objects.all()
        if server_id:
            servers = servers.filter(id=server_id)
        
        if not servers.exists():
            self.stdout.write(self.style.WARNING("No servers found."))
            return
        
        total_calculated = 0
        total_compliant = 0
        total_non_compliant = 0
        errors = 0
        
        for server in servers:
            self.stdout.write(f"Processing server: {server.name} ({server.id})")
            
            for sli_config in sli_configs:
                metric_type = sli_config.metric_type
                
                # Get time window (use SLO config override if available)
                window_days = sli_config.time_window_days
                slo_config = get_slo_config(server, metric_type)
                if slo_config and slo_config.time_window_days:
                    window_days = slo_config.time_window_days
                
                window_start = now - timedelta(days=window_days)
                
                try:
                    # Calculate SLI value
                    sli_value = calculate_sli_value(server, metric_type, window_start, now)
                    
                    # Get SLO config for compliance check
                    if not slo_config:
                        self.stdout.write(
                            f"  {self.style.WARNING('SKIPPED')} {metric_type} - No SLO config found"
                        )
                        continue
                    
                    # Check compliance
                    is_compliant, compliance_percentage = check_compliance(sli_value, slo_config)
                    
                    # Store measurement
                    SLIMeasurement.objects.create(
                        server=server,
                        metric_type=metric_type,
                        time_window_start=window_start,
                        time_window_end=now,
                        sli_value=sli_value,
                        slo_target=slo_config.target_value,
                        is_compliant=is_compliant,
                        compliance_percentage=compliance_percentage,
                        calculated_at=now
                    )
                    
                    total_calculated += 1
                    if is_compliant:
                        total_compliant += 1
                        status = self.style.SUCCESS('COMPLIANT')
                    else:
                        total_non_compliant += 1
                        status = self.style.ERROR('NON-COMPLIANT')
                    
                    self.stdout.write(
                        f"  {status} {metric_type}: SLI={sli_value}, "
                        f"Target={slo_config.target_value}, Compliance={compliance_percentage}%"
                    )
                    
                except Exception as e:
                    errors += 1
                    self.stderr.write(
                        self.style.ERROR(
                            f"  ERROR calculating {metric_type} for {server.name}: {str(e)}"
                        )
                    )
            
            self.stdout.write("")
        
        # Summary
        self.stdout.write("=" * 60)
        self.stdout.write(f"Summary:")
        self.stdout.write(f"  {self.style.SUCCESS('Calculated')}: {total_calculated}")
        self.stdout.write(f"  {self.style.SUCCESS('Compliant')}: {total_compliant}")
        self.stdout.write(f"  {self.style.ERROR('Non-Compliant')}: {total_non_compliant}")
        self.stdout.write(f"  {self.style.ERROR('Errors')}: {errors}")
        self.stdout.write("=" * 60)

