from django.contrib import admin, messages
from django.utils.safestring import mark_safe
from .models import (
    Server, MonitoredLog, LogEvent, AnalysisRule,
    SystemMetric, Anomaly, MonitoringConfig, Service, AggregatedMetric,
    AgentCredential, SyntheticCheck, SyntheticCheckResult,
    SecurityEvent, SecurityMonitorConfig,
    BusinessKPI, BusinessKPIValue, BusinessMonitorConfig, Container, SSHAuthEvent
)


@admin.register(SSHAuthEvent)
class SSHAuthEventAdmin(admin.ModelAdmin):
    list_display = ("server", "timestamp", "success", "username", "source_ip")
    list_filter = ("success", "server")
    search_fields = ("source_ip", "username", "server__name")
    readonly_fields = ("server", "timestamp", "success", "username", "source_ip", "raw")

    def has_add_permission(self, request):
        return False


@admin.register(Container)
class ContainerAdmin(admin.ModelAdmin):
    list_display = ("name", "server", "image", "state", "monitoring_enabled", "last_checked")
    list_filter = ("state", "monitoring_enabled")
    search_fields = ("name", "image", "server__name")
    readonly_fields = ("last_checked", "created_at", "updated_at")


@admin.register(Server)
class ServerAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "ip_address",
        "username",
        "monitoring_status",
    )
    fieldsets = (
        ("Server", {"fields": ("name", "ip_address", "username")}),
    )

    def monitoring_status(self, obj):
        if not obj.pk:
            return "N/A"
        try:
            config = obj.monitoring_config
            status = "✅ Enabled" if config.enabled else "❌ Disabled"
            return mark_safe(f'<span style="color: {"green" if config.enabled else "red"}">{status}</span>')
        except:
            return "❌ Not Configured"
    monitoring_status.short_description = "Monitoring"

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not change:
            MonitoringConfig.objects.get_or_create(
                server=obj,
                defaults={
                    "enabled": True,
                    "collection_interval_seconds": 60,
                    "use_adtk": True,
                    "use_isolation_forest": False,
                    "use_llm_explanation": True,
                    "retention_period_days": 30,
                    "aggregation_enabled": True,
                }
            )
            messages.info(request, f"✅ Monitoring configuration created for {obj.name}.")


@admin.register(MonitoringConfig)
class MonitoringConfigAdmin(admin.ModelAdmin):
    list_display = ("server", "enabled", "collection_interval_seconds", "use_adtk", "use_llm_explanation")
    list_filter = ("enabled", "use_adtk", "aggregation_enabled")


@admin.register(SystemMetric)
class SystemMetricAdmin(admin.ModelAdmin):
    list_display = ("server", "timestamp", "cpu_percent", "memory_percent")
    list_filter = ("server", "timestamp")
    date_hierarchy = "timestamp"


@admin.register(Anomaly)
class AnomalyAdmin(admin.ModelAdmin):
    list_display = ("server", "metric_type", "metric_value", "severity", "timestamp", "resolved")
    list_filter = ("severity", "resolved", "metric_type", "timestamp")
    date_hierarchy = "timestamp"


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ("server", "name", "status", "service_type", "last_checked")
    list_filter = ("status", "service_type", "server")


admin.site.register(MonitoredLog)
admin.site.register(LogEvent)
admin.site.register(AnalysisRule)
admin.site.register(AggregatedMetric)


@admin.register(AgentCredential)
class AgentCredentialAdmin(admin.ModelAdmin):
    """Manage push-agent tokens. The raw token is never shown here -- it is only
    available once, from `manage.py create_agent_token`. This screen is for
    enabling/disabling (revoking) credentials and auditing last use."""
    list_display = ("server", "token_prefix", "enabled", "last_used_at", "last_used_ip", "created_at")
    list_filter = ("enabled",)
    search_fields = ("server__name", "token_prefix")
    readonly_fields = ("token_hash", "token_prefix", "created_at", "last_used_at", "last_used_ip")
    # Don't allow hand-creating credentials here (no way to set a hash safely);
    # use the create_agent_token command instead.
    def has_add_permission(self, request):
        return False


@admin.register(SyntheticCheck)
class SyntheticCheckAdmin(admin.ModelAdmin):
    list_display = ("name", "check_type", "target", "enabled", "last_status", "last_checked_at", "interval_seconds")
    list_filter = ("check_type", "enabled", "last_status")
    search_fields = ("name", "url", "host")
    readonly_fields = ("last_status", "last_checked_at", "consecutive_failures", "consecutive_successes", "last_state_change_at", "created_at", "updated_at")


@admin.register(SyntheticCheckResult)
class SyntheticCheckResultAdmin(admin.ModelAdmin):
    list_display = ("synthetic_check", "timestamp", "success", "status_code", "response_time_ms")
    list_filter = ("success", "synthetic_check")
    readonly_fields = ("synthetic_check", "timestamp", "success", "status_code", "response_time_ms", "error_message")


@admin.register(SecurityEvent)
class SecurityEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "severity", "status", "source_ip", "target_email", "event_count", "last_seen")
    list_filter = ("event_type", "severity", "status")
    search_fields = ("source_ip", "target_email", "title")
    readonly_fields = ("first_seen", "last_seen", "created_at", "updated_at")


@admin.register(SecurityMonitorConfig)
class SecurityMonitorConfigAdmin(admin.ModelAdmin):
    list_display = ("enabled", "alert_enabled", "window_minutes", "brute_force_ip_threshold", "account_failure_threshold")

    def has_add_permission(self, request):
        return not SecurityMonitorConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(BusinessKPI)
class BusinessKPIAdmin(admin.ModelAdmin):
    list_display = ("name", "key", "unit", "direction", "last_value", "last_status", "last_value_at", "enabled")
    list_filter = ("direction", "last_status", "enabled")
    search_fields = ("name", "key")
    readonly_fields = ("last_value", "last_value_at", "last_status", "created_at", "updated_at")


@admin.register(BusinessKPIValue)
class BusinessKPIValueAdmin(admin.ModelAdmin):
    list_display = ("kpi", "value", "source", "timestamp")
    list_filter = ("source", "kpi")
    readonly_fields = ("kpi", "value", "source", "note", "timestamp")

    def has_add_permission(self, request):
        return False


@admin.register(BusinessMonitorConfig)
class BusinessMonitorConfigAdmin(admin.ModelAdmin):
    list_display = ("ingest_token_prefix", "updated_at")
    readonly_fields = ("ingest_token_hash", "ingest_token_prefix", "created_at", "updated_at")

    def has_add_permission(self, request):
        return not BusinessMonitorConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        return False


# Override admin index to redirect to monitoring
from django.shortcuts import redirect
from django.contrib.auth.decorators import login_required

original_index = admin.site.index

@login_required
def custom_admin_index(request, extra_context=None):
    # Redirect authenticated users to monitoring
    return redirect("/monitoring/")

admin.site.index = custom_admin_index
