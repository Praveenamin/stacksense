import hashlib
import secrets

from django.db import models
from django.utils import timezone
from django.contrib.auth.models import User

from .alert_categories import AlertCategory


class Server(models.Model):
    class Meta:
        verbose_name = "Link Connection"
        verbose_name_plural = "Link Connections"

    name = models.CharField(max_length=100)
    ip_address = models.GenericIPAddressField()
    username = models.CharField(max_length=100)
    suppress_alerts = models.BooleanField(default=False, help_text="Whether to suppress email alerts for this server")
    suspend_monitoring = models.BooleanField(default=False, help_text="Whether to suspend monitoring for this server")

    def __str__(self):
        return self.name


class SystemMetric(models.Model):
    """Stores collected system metrics"""
    server = models.ForeignKey(Server, on_delete=models.CASCADE, related_name="metrics")
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    
    # CPU metrics
    cpu_percent = models.FloatField()
    cpu_count = models.IntegerField(null=True, blank=True, help_text="Logical CPU count")
    physical_cpu_count = models.IntegerField(null=True, blank=True, help_text="Physical CPU cores")
    cpu_load_avg_1m = models.FloatField(null=True, blank=True)
    cpu_load_avg_5m = models.FloatField(null=True, blank=True)
    cpu_load_avg_15m = models.FloatField(null=True, blank=True)
    
    # Memory metrics
    memory_total = models.BigIntegerField()  # bytes
    memory_available = models.BigIntegerField()  # bytes
    memory_percent = models.FloatField()
    memory_used = models.BigIntegerField()  # bytes
    memory_buffers = models.BigIntegerField(null=True, blank=True)  # bytes
    memory_cached = models.BigIntegerField(null=True, blank=True)  # bytes
    memory_shared = models.BigIntegerField(null=True, blank=True)  # bytes
    swap_total = models.BigIntegerField(null=True, blank=True)
    swap_used = models.BigIntegerField(null=True, blank=True)
    swap_percent = models.FloatField(null=True, blank=True)
    
    # Disk metrics (JSON field for multiple disks)
    # Enhanced structure: {"/": {"total": ..., "used": ..., "percent": ..., "disk_type": "SSD", "raid": "none", "physical_disk": "sda"}}
    disk_usage = models.JSONField(default=dict)

    # Physical disk inventory pushed by the agent (read-only lsblk/sys inventory):
    # {"physical_disk_count": N, "disks": [{name, type(SSD/HDD/NVMe), model, size, transport, rotational}],
    #  "raid_arrays": [{name, level, size}], "raid": "raid1"|"none"}
    disk_hardware = models.JSONField(default=dict, blank=True)

    # Network metrics (JSON field for multiple interfaces)
    network_io = models.JSONField(default=dict)
    network_connections = models.IntegerField(null=True, blank=True)

    # I/O rate metrics (bytes per second)
    disk_io_read = models.BigIntegerField(null=True, blank=True, help_text="Disk read rate (bytes/second)")
    disk_io_write = models.BigIntegerField(null=True, blank=True, help_text="Disk write rate (bytes/second)")
    net_io_sent = models.BigIntegerField(null=True, blank=True, help_text="Network sent rate (bytes/second)")
    net_io_recv = models.BigIntegerField(null=True, blank=True, help_text="Network received rate (bytes/second)")
    
    # Network utilization metrics (percentage of NIC max speed)
    net_utilization_sent = models.FloatField(null=True, blank=True, help_text="Network send utilization % (based on NIC max speed)")
    net_utilization_recv = models.FloatField(null=True, blank=True, help_text="Network receive utilization % (based on NIC max speed)")
    nic_max_speed_bits = models.BigIntegerField(null=True, blank=True, help_text="Total NIC max speed in bits/second (all interfaces)")
    
    # Raw I/O counter values for rate calculation (cumulative totals across all disks)
    disk_read_bytes_total = models.BigIntegerField(null=True, blank=True, help_text="Cumulative disk read bytes (all disks)")
    disk_write_bytes_total = models.BigIntegerField(null=True, blank=True, help_text="Cumulative disk write bytes (all disks)")
    
    # System uptime
    system_uptime_seconds = models.BigIntegerField(null=True, blank=True, help_text="System uptime in seconds (time since last boot)")
    
    # Process context (collected during normal metric collection)
    top_processes = models.JSONField(
        null=True,
        blank=True,
        help_text="Top processes by CPU/Memory at collection time. Format: {'cpu': [...], 'memory': [...]}"
    )
    ipc_stats = models.JSONField(
        null=True,
        blank=True,
        help_text="SysV IPC / POSIX shared-memory summary for leak detection. "
                  "Keys: shm_segments, shm_bytes, shm_orphaned, shm_orphaned_bytes, "
                  "sem_arrays, msg_queues, msg_bytes, devshm_bytes."
    )
    
    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["server", "-timestamp"]),
        ]

    def __str__(self):
        return f"{self.server.name} - {self.timestamp}"


class Anomaly(models.Model):
    """Detected anomalies in system metrics"""
    class Severity(models.TextChoices):
        LOW = "LOW", "Low"
        MEDIUM = "MEDIUM", "Medium"
        HIGH = "HIGH", "High"
        CRITICAL = "CRITICAL", "Critical"
    
    server = models.ForeignKey(Server, on_delete=models.CASCADE, related_name="anomalies")
    metric = models.ForeignKey(SystemMetric, on_delete=models.CASCADE, related_name="anomalies")
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    
    metric_type = models.CharField(max_length=50)  # "cpu", "memory", "disk", "network"
    metric_name = models.CharField(max_length=100)  # e.g., "cpu_percent", "memory_percent"
    metric_value = models.FloatField()
    anomaly_score = models.FloatField()  # IsolationForest score or ADTK score
    severity = models.CharField(max_length=20, choices=Severity.choices, default=Severity.MEDIUM)
    
    # LLM-generated explanation (optional)
    explanation = models.TextField(blank=True)
    llm_generated = models.BooleanField(default=False)
    
    # Status
    acknowledged = models.BooleanField(default=False)
    resolved = models.BooleanField(default=False)
    resolved_at = models.DateTimeField(null=True, blank=True)
    # When the metric actually returned to normal (auto-detected by the detector),
    # independent of when an admin acknowledged it. Used for the true incident duration.
    recovered_at = models.DateTimeField(null=True, blank=True, db_index=True)
    admin_note = models.TextField(blank=True, default="", help_text="Admin's note / reason recorded when resolving")
    resolved_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="resolved_anomalies")

    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["server", "-timestamp"]),
            models.Index(fields=["severity", "-timestamp"]),
        ]

    def __str__(self):
        return f"{self.server.name} - {self.metric_type} anomaly at {self.timestamp}"


class Service(models.Model):
    """Detected services on servers"""
    server = models.ForeignKey(Server, on_delete=models.CASCADE, related_name="services")
    name = models.CharField(max_length=100)
    status = models.CharField(max_length=50, default="unknown")  # running, stopped, failed
    service_type = models.CharField(max_length=50, default="systemd")  # systemd, process, port
    port = models.IntegerField(null=True, blank=True)
    bind_address = models.CharField(max_length=50, null=True, blank=True, help_text="IP address the service is bound to (e.g., 0.0.0.0, 127.0.0.1)")
    process_id = models.CharField(max_length=50, null=True, blank=True)
    last_checked = models.DateTimeField(default=timezone.now)
    monitoring_enabled = models.BooleanField(default=False, help_text="Whether monitoring is enabled for this service")
    auto_detected = models.BooleanField(default=False, help_text="Whether this service was auto-detected from port scan")
    # Friendly UI label, e.g. "nginx (:80)". The stable identity stays in `name`
    # (e.g. "port-80") so the (server, name) upsert key and alert markers never churn
    # when a banner read flaps; `display_name` is purely cosmetic and may be NULL.
    display_name = models.CharField(max_length=150, null=True, blank=True, help_text="Friendly UI label; identity stays in name")
    # How this service was identified: systemd | port-banner | port-map | port-unknown
    detected_via = models.CharField(max_length=30, null=True, blank=True, help_text="Detection provenance")

    class Meta:
        unique_together = [["server", "name"]]
        indexes = [
            models.Index(fields=["server", "status"]),
            models.Index(fields=["last_checked"]),
        ]

    def __str__(self):
        return f"{self.name} on {self.server.name} ({self.status})"

    @property
    def label(self):
        """Human label for the UI. Precedence: agent-supplied display_name, then a
        role-from-port name ("HTTP (:80)"), then the raw identity name."""
        if self.display_name:
            return self.display_name
        if self.service_type == "port" and self.port:
            from .port_roles import role_for_port
            role = role_for_port(self.port)
            if role:
                return f"{role} (:{self.port})"
        return self.name


class MonitoringConfig(models.Model):
    """Configuration for monitoring each server"""
    server = models.OneToOneField(Server, on_delete=models.CASCADE, related_name="monitoring_config")
    
    # Collection settings
    collection_interval_seconds = models.IntegerField(default=60, help_text="Seconds between metric collections")
    enabled = models.BooleanField(default=True)
    
    # Anomaly detection settings
    class AnomalySensitivity(models.TextChoices):
        OFF = "OFF", "Off"
        LOW = "LOW", "Low"
        BALANCED = "BALANCED", "Balanced"
        HIGH = "HIGH", "High"

    anomaly_sensitivity = models.CharField(
        max_length=10,
        choices=AnomalySensitivity.choices,
        default=AnomalySensitivity.BALANCED,
        help_text="Anomaly detection sensitivity. 'Off' disables detection for this server.",
    )
    use_adtk = models.BooleanField(default=True, help_text="Use ADTK (primary) vs IsolationForest (fallback)")
    use_isolation_forest = models.BooleanField(default=False, help_text="Use IsolationForest (fallback)")
    contamination = models.FloatField(default=0.1, help_text="Expected proportion of anomalies (0.0-0.5)")
    window_size = models.IntegerField(default=100, help_text="Number of recent metrics for training")
    
    # ADTK-specific settings
    adtk_threshold_factor = models.FloatField(default=2.0, help_text="Threshold factor for ADTK detectors")
    adtk_window_size = models.IntegerField(default=30, help_text="Window size for ADTK time-series analysis")
    
    # Thresholds (fallback if ML fails)
    cpu_threshold = models.FloatField(default=80.0, help_text="CPU usage threshold (%)")
    memory_threshold = models.FloatField(default=90.0, help_text="Memory usage threshold (%)")
    disk_threshold = models.FloatField(default=90.0, help_text="Disk usage threshold (%)")
    disk_io_threshold = models.FloatField(default=1000.0, help_text="Disk I/O threshold (MB/s)")
    network_io_threshold = models.FloatField(default=1000.0, help_text="Network I/O threshold (MB/s)")
    
    # Selected disk partitions to monitor (JSON array of mount points)
    monitored_disks = models.JSONField(default=list, help_text="List of disk mount points to monitor (e.g., ['/', '/home'])")
    
    # LLM settings
    use_llm_explanation = models.BooleanField(default=False, help_text="Optionally add an LLM explanation on top of the built-in deterministic one")
    
    # Data retention settings
    retention_period_days = models.IntegerField(default=30, help_text="Days to keep raw metrics before deletion")
    aggregation_enabled = models.BooleanField(default=True, help_text="Enable metric aggregation")
    
    # Alert and monitoring control
    alert_suppressed = models.BooleanField(default=False, help_text="Suppress alerts for this server")
    monitoring_suspended = models.BooleanField(default=False, help_text="Suspend monitoring for this server")

    # Service monitoring settings
    monitored_services = models.JSONField(default=list, help_text="List of systemd services to monitor")
    service_failure_alert = models.BooleanField(default=True, help_text="Enable alerts for service failures")
    service_restart_threshold = models.IntegerField(default=2, help_text="Number of restarts allowed in 10 minutes")
    service_down_duration_threshold = models.IntegerField(default=30, help_text="Seconds before down service triggers alert")
    
    class Meta:
        verbose_name = "Monitoring Configuration"
        verbose_name_plural = "Monitoring Configurations"

    def __str__(self):
        return f"Monitoring config for {self.server.name}"


class AggregatedMetric(models.Model):
    """Aggregated metrics for long-term storage"""
    server = models.ForeignKey(Server, on_delete=models.CASCADE, related_name="aggregated_metrics")
    aggregation_type = models.CharField(max_length=20)  # "hourly", "daily"
    timestamp = models.DateTimeField(db_index=True)
    
    # Aggregated values
    cpu_avg = models.FloatField(null=True, blank=True)
    cpu_min = models.FloatField(null=True, blank=True)
    cpu_max = models.FloatField(null=True, blank=True)
    
    memory_avg = models.FloatField(null=True, blank=True)
    memory_min = models.FloatField(null=True, blank=True)
    memory_max = models.FloatField(null=True, blank=True)
    
    disk_avg = models.FloatField(null=True, blank=True)
    disk_min = models.FloatField(null=True, blank=True)
    disk_max = models.FloatField(null=True, blank=True)
    
    metric_count = models.IntegerField(default=0, help_text="Number of raw metrics aggregated")
    
    class Meta:
        unique_together = [["server", "aggregation_type", "timestamp"]]
        indexes = [
            models.Index(fields=["server", "aggregation_type", "-timestamp"]),
        ]
    
    def __str__(self):
        return f"{self.server.name} - {self.aggregation_type} - {self.timestamp}"

class EmailAlertConfig(models.Model):
    """Email configuration for sending alerts"""
    class ProviderChoices(models.TextChoices):
        GMAIL = "gmail", "Gmail"
        OUTLOOK = "outlook", "Outlook / Office365"
        YAHOO = "yahoo", "Yahoo"
        CUSTOM = "custom", "Custom SMTP"

    provider = models.CharField(
        max_length=20,
        choices=ProviderChoices.choices,
        default=ProviderChoices.CUSTOM
    )
    smtp_host = models.CharField(max_length=255, blank=True)
    smtp_port = models.IntegerField(default=587)
    use_tls = models.BooleanField(default=True)
    use_ssl = models.BooleanField(default=False)
    username = models.EmailField(blank=True)
    password = models.CharField(max_length=255, blank=True)  # Will be encrypted
    from_email = models.EmailField(blank=True)
    # Recipients are no longer a single field -- they are resolved per alert from the
    # role-based routing matrix (see AlertRoutingRule / core.alert_routing).
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        # The sender is ALWAYS the authenticated SMTP account -- we never send "as" an
        # arbitrary address (anti-spoofing). Keep from_email pinned to the username so
        # every alert/test/notification goes out from the known, owned sender.
        self.from_email = self.username
        super().save(*args, **kwargs)

    def get_smtp_config(self):
        """Get SMTP configuration with provider defaults"""
        configs = {
            self.ProviderChoices.GMAIL: {
                'smtp_host': 'smtp.gmail.com',
                'smtp_port': 587,
                'use_tls': True,
                'use_ssl': False,
            },
            self.ProviderChoices.OUTLOOK: {
                'smtp_host': 'smtp.office365.com',
                'smtp_port': 587,
                'use_tls': True,
                'use_ssl': False,
            },
            self.ProviderChoices.YAHOO: {
                'smtp_host': 'smtp.mail.yahoo.com',
                'smtp_port': 465,
                'use_tls': False,
                'use_ssl': True,
            },
            self.ProviderChoices.CUSTOM: {
                'smtp_host': self.smtp_host,
                'smtp_port': self.smtp_port,
                'use_tls': self.use_tls,
                'use_ssl': self.use_ssl,
            }
        }
        return configs[self.provider]
    
    def __str__(self):
        return f"Email Alert Config ({self.provider})"


class SlackAlertConfig(models.Model):
    """Slack configuration for sending alerts via Incoming Webhooks"""
    webhook_url = models.CharField(
        max_length=500,
        help_text="Slack Incoming Webhook URL (starts with https://hooks.slack.com/)"
    )
    channel = models.CharField(
        max_length=100,
        blank=True,
        help_text="Channel to send messages to (e.g., #alerts or @username). Optional if webhook already specifies channel."
    )
    username = models.CharField(
        max_length=100,
        blank=True,
        help_text="Bot username to display in Slack (optional)"
    )
    icon_emoji = models.CharField(
        max_length=50,
        blank=True,
        default=":warning:",
        help_text="Emoji icon for bot messages (e.g., :robot_face:, :warning:)"
    )
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Slack Alert Config ({self.channel or 'default channel'})"


class AppConfig(models.Model):
    """Application-wide configuration settings (single instance)"""
    class LanguageChoices(models.TextChoices):
        ENGLISH = "en", "English"

    display_timezone = models.CharField(
        max_length=100,
        default="UTC",
        help_text="Timezone for displaying all timestamps (e.g., 'Asia/Kolkata', 'America/New_York')"
    )
    language = models.CharField(
        max_length=10,
        choices=LanguageChoices.choices,
        default=LanguageChoices.ENGLISH,
        help_text="Application language"
    )
    data_retention_days = models.PositiveSmallIntegerField(
        default=60,
        help_text="Keep collected data (metrics, incidents, logs) for this many days; "
                  "older data is pruned automatically. Min 7, max 365.",
    )
    # First-run setup wizard state.
    setup_completed = models.BooleanField(
        default=False,
        help_text="True once the first-run setup wizard has created the initial admin.",
    )
    base_url = models.URLField(
        blank=True, default="",
        help_text="Public base URL of this instance (used for absolute links in emails/UI).",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = "Application Configuration"
        verbose_name_plural = "Application Configuration"
    
    def clean(self):
        """Validate timezone name"""
        from django.core.exceptions import ValidationError
        import pytz
        try:
            pytz.timezone(self.display_timezone)
        except pytz.UnknownTimeZoneError:
            raise ValidationError(f"Invalid timezone: {self.display_timezone}")
        try:
            drd = int(self.data_retention_days)
        except (TypeError, ValueError):
            raise ValidationError("Data retention must be a whole number of days.")
        if not (7 <= drd <= 365):
            raise ValidationError("Data retention must be between 7 and 365 days.")

    def save(self, *args, **kwargs):
        """Override save to ensure single instance and validate timezone"""
        self.full_clean()  # Run validation
        # Force id=1 for single instance pattern
        self.id = 1
        super().save(*args, **kwargs)
        # Invalidate cache when timezone changes (gracefully handle cache errors)
        try:
            from django.core.cache import cache
            cache.delete('app_display_timezone')
        except Exception:
            # If cache fails (e.g., Redis not available), continue without cache
            pass
    
    @classmethod
    def get_config(cls):
        """Get or create the single AppConfig instance"""
        config, created = cls.objects.get_or_create(
            id=1,
            defaults={
                'display_timezone': 'UTC',
                'language': cls.LanguageChoices.ENGLISH,
                'data_retention_days': 60,
            }
        )
        return config
    
    def __str__(self):
        return f"App Config (Timezone: {self.display_timezone}, Language: {self.language})"


class AlertHistory(models.Model):
    """History of alerts sent and resolved"""
    class AlertType(models.TextChoices):
        CPU = "CPU", "CPU"
        MEMORY = "Memory", "Memory"
        DISK = "Disk", "Disk"
        CONNECTION = "CONNECTION", "Connection"
        SERVICE = "SERVICE", "Service"
        CONTAINER = "CONTAINER", "Container"

    class AlertStatus(models.TextChoices):
        TRIGGERED = "triggered", "Triggered"
        RESOLVED = "resolved", "Resolved"
    
    server = models.ForeignKey(Server, on_delete=models.CASCADE, related_name="alert_history")
    alert_type = models.CharField(max_length=20, choices=AlertType.choices)
    status = models.CharField(max_length=20, choices=AlertStatus.choices, default=AlertStatus.TRIGGERED)
    severity = models.CharField(max_length=20, choices=Anomaly.Severity.choices, default=Anomaly.Severity.HIGH,
                                help_text="Severity for routing/grouping (connection down=CRITICAL, threshold/service/container=HIGH, resolved=LOW)")
    value = models.FloatField(help_text="Current metric value")
    threshold = models.FloatField(help_text="Threshold value")
    message = models.TextField()
    recipients = models.TextField(help_text="Comma-separated list of email recipients")
    sent_at = models.DateTimeField(default=timezone.now, db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    admin_note = models.TextField(blank=True, default="", help_text="Admin's note / reason recorded when resolving")
    resolved_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="resolved_alerts")
    process_context = models.JSONField(
        null=True,
        blank=True,
        help_text="Top processes at time of alert. Format: {'cpu': [...], 'memory': [...]}"
    )
    
    class Meta:
        ordering = ["-sent_at"]
        indexes = [
            models.Index(fields=["server", "-sent_at"]),
            models.Index(fields=["status", "-sent_at"]),
        ]
        verbose_name = "Alert History"
        verbose_name_plural = "Alert History"
    
    def __str__(self):
        return f"{self.server.name} - {self.alert_type} {self.status} at {self.sent_at}"


class Role(models.Model):
    """RBAC Role - defines a set of permissions that can be assigned to users"""
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True, help_text="Description of this role's responsibilities")
    is_protected = models.BooleanField(default=False, help_text="Protected roles cannot be renamed or deleted")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Role"
        verbose_name_plural = "Roles"
        ordering = ['name']

    def __str__(self):
        return self.name

    def get_privileges(self):
        """Get all privileges for this role"""
        return self.role_privileges.select_related('privilege')


class Privilege(models.Model):
    """Individual permission that can be assigned to roles"""
    key = models.CharField(max_length=100, unique=True, help_text="Machine-readable key (e.g., 'view_dashboard')")
    label = models.CharField(max_length=200, help_text="Human-readable label (e.g., 'View Dashboard')")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Privilege"
        verbose_name_plural = "Privileges"
        ordering = ['key']

    def __str__(self):
        return self.label


class RolePrivilege(models.Model):
    """Many-to-many mapping between roles and privileges"""
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name='role_privileges')
    privilege = models.ForeignKey(Privilege, on_delete=models.CASCADE, related_name='role_privileges')

    class Meta:
        unique_together = [['role', 'privilege']]
        verbose_name = "Role Privilege"
        verbose_name_plural = "Role Privileges"

    def __str__(self):
        return f"{self.role.name} - {self.privilege.label}"


class UserACL(models.Model):
    """Access Control List for Staff Users - now uses role-based permissions"""
    class DashboardView(models.TextChoices):
        OPERATIONS = "operations", "Operations"
        EXECUTIVE = "executive", "Executive"

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='acl')
    role = models.ForeignKey(Role, on_delete=models.SET_NULL, null=True, blank=True,
                           help_text="Role that defines this user's permissions")
    dashboard_view = models.CharField(
        max_length=20, choices=DashboardView.choices, default=DashboardView.OPERATIONS,
        help_text="Which dashboard perspective this user sees (Operations or Executive)",
    )

    # DEPRECATED: These boolean flags are kept for backward compatibility
    # but should be removed after migration to role-based system
    can_view_dashboard = models.BooleanField(default=True, help_text="[DEPRECATED] Use role privileges")
    can_edit_thresholds = models.BooleanField(default=False, help_text="[DEPRECATED] Use role privileges")
    can_halt_monitoring = models.BooleanField(default=False, help_text="[DEPRECATED] Use role privileges")
    can_mute_notifications = models.BooleanField(default=False, help_text="[DEPRECATED] Use role privileges")
    can_add_server = models.BooleanField(default=False, help_text="[DEPRECATED] Use role privileges")
    can_edit_server = models.BooleanField(default=False, help_text="[DEPRECATED] Use role privileges")
    can_delete_server = models.BooleanField(default=False, help_text="[DEPRECATED] Use role privileges")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "User ACL"
        verbose_name_plural = "User ACLs"
        ordering = ['user__username']

    def __str__(self):
        role_name = self.role.name if self.role else "No Role"
        return f"ACL for {self.user.username} ({role_name})"

    def has_privilege(self, privilege_key):
        """Check if user has a specific privilege"""
        # Root Admin (superuser) has all privileges
        if self.user.is_superuser:
            return True

        # Check role-based privileges
        if self.role and self.role.role_privileges.filter(privilege__key=privilege_key).exists():
            return True

        return False

    def get_all_privileges(self):
        """Get all privilege keys for this user"""
        if self.user.is_superuser:
            # Root Admin has all privileges
            return Privilege.objects.values_list('key', flat=True)

        if self.role:
            return self.role.role_privileges.values_list('privilege__key', flat=True)

        return []

    @classmethod
    def get_or_create_for_user(cls, user):
        """Get or create ACL for a user"""
        acl, created = cls.objects.get_or_create(user=user)
        if created:
            # Superusers -> Admin; other staff -> Operator (safe read-only default).
            # Superusers still get all privileges via the superuser bypass even if
            # the role rows aren't seeded yet.
            default_name = "Admin" if user.is_superuser else "Operator"
            try:
                acl.role = Role.objects.get(name=default_name)
                acl.save()
            except Role.DoesNotExist:
                pass
        return acl


class AlertRoutingRule(models.Model):
    """Role-based alert routing: one row per (Role x Alert Category) saying the minimum
    severity at which users in that role get emailed. OFF means that role never receives
    that category. This replaces the single EmailAlertConfig.to_email -- recipients are
    resolved at send time from the alert's (category, severity)."""
    OFF = "OFF"
    MIN_SEVERITY_CHOICES = [
        (OFF, "Off"),
        ("LOW", "Low and above"),
        ("MEDIUM", "Medium and above"),
        ("HIGH", "High and above"),
        ("CRITICAL", "Critical only"),
    ]

    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="alert_routes")
    category = models.CharField(max_length=20, choices=AlertCategory.choices)
    min_severity = models.CharField(max_length=20, choices=MIN_SEVERITY_CHOICES, default=OFF,
                                    help_text="Lowest severity that triggers email for this role+category (OFF = never)")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [["role", "category"]]
        verbose_name = "Alert Routing Rule"
        verbose_name_plural = "Alert Routing Rules"
        ordering = ["role__name", "category"]

    def __str__(self):
        return f"{self.role.name} / {self.category} >= {self.min_severity}"


class ServerHeartbeat(models.Model):
    """Tracks heartbeat signals from agent scripts on monitored servers"""
    server = models.ForeignKey(Server, on_delete=models.CASCADE, related_name="heartbeats")
    last_heartbeat = models.DateTimeField(default=timezone.now, db_index=True, help_text="Last heartbeat timestamp from agent")
    agent_version = models.CharField(max_length=50, blank=True, null=True, help_text="Optional agent version string")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = "Server Heartbeat"
        verbose_name_plural = "Server Heartbeats"
        indexes = [
            models.Index(fields=["server", "-last_heartbeat"]),
            models.Index(fields=["-last_heartbeat"]),
        ]
        unique_together = [["server"]]
    
    def __str__(self):
        return f"Heartbeat for {self.server.name} - {self.last_heartbeat}"


class AgentVersion(models.Model):
    """Track monitoring agent versions per server"""
    server = models.ForeignKey(Server, on_delete=models.CASCADE, related_name="agent_versions")
    version = models.CharField(max_length=50, help_text="Agent version string (e.g., 'v2.4.1')")
    last_seen = models.DateTimeField(default=timezone.now, db_index=True, help_text="Last time this version was reported")
    
    class Meta:
        verbose_name = "Agent Version"
        verbose_name_plural = "Agent Versions"
        unique_together = [["server", "version"]]
        indexes = [
            models.Index(fields=["-last_seen"]),
            models.Index(fields=["version"]),
        ]
        ordering = ["-last_seen"]
    
    def __str__(self):
        return f"{self.server.name} - {self.version}"


class LoginActivity(models.Model):
    """Track user login/authentication events"""
    class StatusChoices(models.TextChoices):
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"
    
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="login_activities")
    email = models.CharField(max_length=255, help_text="Email address used for login attempt")
    ip_address = models.GenericIPAddressField(help_text="IP address of the login attempt")
    location = models.CharField(max_length=255, blank=True, null=True, help_text="Geographical location (e.g., 'San Francisco, US')")
    status = models.CharField(max_length=20, choices=StatusChoices.choices, help_text="Login attempt status")
    timestamp = models.DateTimeField(default=timezone.now, db_index=True, help_text="When the login attempt occurred")
    
    class Meta:
        verbose_name = "Login Activity"
        verbose_name_plural = "Login Activities"
        indexes = [
            models.Index(fields=["-timestamp"]),
            models.Index(fields=["status", "-timestamp"]),
            models.Index(fields=["email", "-timestamp"]),
        ]
        ordering = ["-timestamp"]
    
    def __str__(self):
        return f"{self.email} - {self.status} - {self.timestamp}"


class SLIConfig(models.Model):
    """Configuration for Service Level Indicator (SLI) metric definitions"""
    class MetricType(models.TextChoices):
        UPTIME = "UPTIME", "Uptime"
        CPU = "CPU", "CPU"
        MEMORY = "MEMORY", "Memory"
        DISK = "DISK", "Disk"
        NETWORK = "NETWORK", "Network"
        RESPONSE_TIME = "RESPONSE_TIME", "Response Time"
        ERROR_RATE = "ERROR_RATE", "Error Rate"
    
    metric_type = models.CharField(
        max_length=50,
        choices=MetricType.choices,
        unique=True,
        help_text="Type of metric for this SLI"
    )
    calculation_method = models.CharField(
        max_length=50,
        default="percentage",
        help_text="How to calculate this SLI (e.g., 'percentage', 'average', 'percentile')"
    )
    time_window_days = models.IntegerField(
        default=7,
        help_text="Default time window in days for measurement (7, 30, 90)"
    )
    enabled = models.BooleanField(
        default=True,
        help_text="Whether this SLI is enabled"
    )
    description = models.TextField(
        blank=True,
        help_text="Human-readable description of this SLI"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = "SLI Configuration"
        verbose_name_plural = "SLI Configurations"
        ordering = ['metric_type']
    
    def __str__(self):
        return f"{self.get_metric_type_display()} SLI"


class SLOConfig(models.Model):
    """Service Level Objective (SLO) configuration with global defaults and per-server overrides"""
    class MetricType(models.TextChoices):
        UPTIME = "UPTIME", "Uptime"
        CPU = "CPU", "CPU"
        MEMORY = "MEMORY", "Memory"
        DISK = "DISK", "Disk"
        NETWORK = "NETWORK", "Network"
        RESPONSE_TIME = "RESPONSE_TIME", "Response Time"
        ERROR_RATE = "ERROR_RATE", "Error Rate"
    
    class TargetOperator(models.TextChoices):
        GTE = "gte", "Greater than or equal (≥)"
        LTE = "lte", "Less than or equal (≤)"
        EQ = "eq", "Equal (=)"
    
    server = models.ForeignKey(
        Server,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="slo_configs",
        help_text="Server for this SLO override (null for global default)"
    )
    metric_type = models.CharField(
        max_length=50,
        choices=MetricType.choices,
        help_text="Type of metric for this SLO"
    )
    target_value = models.FloatField(
        help_text="Target value (e.g., 99.9 for 99.9% uptime, 200 for 200ms response time)"
    )
    target_operator = models.CharField(
        max_length=10,
        choices=TargetOperator.choices,
        default=TargetOperator.GTE,
        help_text="Comparison operator (e.g., 'gte' for uptime >= 99.9%)"
    )
    time_window_days = models.IntegerField(
        null=True,
        blank=True,
        help_text="Time window in days for this SLO (overrides SLIConfig default if set)"
    )
    enabled = models.BooleanField(
        default=True,
        help_text="Whether this SLO is enabled"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = "SLO Configuration"
        verbose_name_plural = "SLO Configurations"
        unique_together = [["server", "metric_type"]]
        indexes = [
            models.Index(fields=["server", "metric_type"]),
            models.Index(fields=["metric_type"]),
        ]
        ordering = ['metric_type']
    
    def __str__(self):
        server_name = self.server.name if self.server else "Global"
        return f"{server_name} - {self.get_metric_type_display()} SLO ({self.target_value})"


class ServiceLatencyMeasurement(models.Model):
    """Latency measurements for monitored services"""
    class MeasurementType(models.TextChoices):
        HTTP = "HTTP", "HTTP"
        TCP = "TCP", "TCP"
        MYSQL = "MYSQL", "MySQL"
        SSH_LOCAL = "SSH_LOCAL", "SSH Local (localhost-bound)"
        OTHER = "OTHER", "Other"
    
    service = models.ForeignKey(
        Service,
        on_delete=models.CASCADE,
        related_name="latency_measurements",
        help_text="Service for which latency is measured"
    )
    latency_ms = models.FloatField(
        help_text="Measured latency in milliseconds"
    )
    timestamp = models.DateTimeField(
        default=timezone.now,
        db_index=True,
        help_text="When measurement was taken"
    )
    success = models.BooleanField(
        default=True,
        help_text="Whether measurement succeeded"
    )
    error_message = models.TextField(
        null=True,
        blank=True,
        help_text="Error details if measurement failed"
    )
    measurement_type = models.CharField(
        max_length=20,
        choices=MeasurementType.choices,
        default=MeasurementType.HTTP,
        help_text="How latency was measured"
    )
    
    class Meta:
        verbose_name = "Service Latency Measurement"
        verbose_name_plural = "Service Latency Measurements"
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["service", "-timestamp"]),
            models.Index(fields=["timestamp"]),
        ]
    
    def __str__(self):
        return f"{self.service.name} - {self.latency_ms}ms at {self.timestamp}"


class SLIMeasurement(models.Model):
    """Calculated SLI values and compliance status"""
    class MetricType(models.TextChoices):
        UPTIME = "UPTIME", "Uptime"
        CPU = "CPU", "CPU"
        MEMORY = "MEMORY", "Memory"
        DISK = "DISK", "Disk"
        NETWORK = "NETWORK", "Network"
        RESPONSE_TIME = "RESPONSE_TIME", "Response Time"
        ERROR_RATE = "ERROR_RATE", "Error Rate"
    
    server = models.ForeignKey(
        Server,
        on_delete=models.CASCADE,
        related_name="sli_measurements",
        help_text="Server for this measurement"
    )
    metric_type = models.CharField(
        max_length=50,
        choices=MetricType.choices,
        help_text="Type of metric"
    )
    time_window_start = models.DateTimeField(
        help_text="Start of time window for this measurement"
    )
    time_window_end = models.DateTimeField(
        db_index=True,
        help_text="End of time window for this measurement"
    )
    sli_value = models.FloatField(
        help_text="Calculated SLI value"
    )
    slo_target = models.FloatField(
        help_text="Target value from SLOConfig"
    )
    is_compliant = models.BooleanField(
        default=False,
        help_text="Whether sli_value meets SLO target"
    )
    compliance_percentage = models.FloatField(
        null=True,
        blank=True,
        help_text="Compliance percentage (e.g., 98.5)"
    )
    calculated_at = models.DateTimeField(
        default=timezone.now,
        db_index=True,
        help_text="When this measurement was calculated"
    )
    
    class Meta:
        verbose_name = "SLI Measurement"
        verbose_name_plural = "SLI Measurements"
        ordering = ["-time_window_end", "-calculated_at"]
        indexes = [
            models.Index(fields=["server", "metric_type", "-time_window_end"]),
            models.Index(fields=["server", "-time_window_end"]),
            models.Index(fields=["metric_type", "-time_window_end"]),
        ]
    
    def __str__(self):
        return f"{self.server.name} - {self.get_metric_type_display()} SLI: {self.sli_value} (target: {self.slo_target})"


class AgentCredential(models.Model):
    """Per-server credential for the push-based monitoring agent.

    The agent on each monitored VM authenticates to the ingest API with a bearer
    token. Only a SHA-256 hash of the token is stored here -- the raw token is
    shown exactly once at creation time and cannot be recovered afterwards.

    Each server has its own token. A leaked token therefore only allows posting
    metrics for that single server and grants NO access into any machine, which
    is the core of the push model's security: the monitoring server holds no
    credentials that can log in to the fleet.
    """
    server = models.OneToOneField(
        Server,
        on_delete=models.CASCADE,
        related_name="agent_credential",
        help_text="The monitored server this token authenticates",
    )
    token_hash = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        help_text="SHA-256 hex digest of the agent token (the raw token is never stored)",
    )
    token_prefix = models.CharField(
        max_length=12,
        blank=True,
        help_text="First few characters of the token, for identification only",
    )
    enabled = models.BooleanField(
        default=True,
        help_text="Whether this token is currently accepted by the ingest API",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Last time a valid push was received with this token",
    )
    last_used_ip = models.GenericIPAddressField(
        null=True,
        blank=True,
        help_text="Source IP of the last accepted push",
    )

    class Meta:
        verbose_name = "Agent Credential"
        verbose_name_plural = "Agent Credentials"

    def __str__(self):
        return f"Agent credential for {self.server.name}"

    @staticmethod
    def hash_token(raw_token):
        """Return the SHA-256 hex digest used to store/look up a token."""
        return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    @classmethod
    def generate_for_server(cls, server):
        """Create or rotate the credential for a server.

        Returns a tuple of (credential, raw_token). The raw_token is the only
        time the plaintext token is available -- surface it to the operator once
        and never persist it.
        """
        raw_token = secrets.token_urlsafe(32)
        cred, _ = cls.objects.update_or_create(
            server=server,
            defaults={
                "token_hash": cls.hash_token(raw_token),
                "token_prefix": raw_token[:8],
                "enabled": True,
                "last_used_at": None,
                "last_used_ip": None,
            },
        )
        return cred, raw_token

    @classmethod
    def authenticate(cls, raw_token):
        """Return the enabled credential matching a raw token, or None.

        The lookup is performed on the indexed hash (not the raw token), so it
        does not leak the secret via timing.
        """
        if not raw_token:
            return None
        try:
            return cls.objects.select_related("server").get(
                token_hash=cls.hash_token(raw_token),
                enabled=True,
            )
        except cls.DoesNotExist:
            return None


class SyntheticCheck(models.Model):
    """A synthetic (bot-driven) uptime check the monitoring server runs on a
    schedule against a URL or TCP port. Part of User Experience monitoring:
    catch outages and slowdowns before real users do."""

    class CheckType(models.TextChoices):
        HTTP = "HTTP", "HTTP / HTTPS"
        TCP = "TCP", "TCP Port"

    class Status(models.TextChoices):
        UP = "UP", "Up"
        DOWN = "DOWN", "Down"
        UNKNOWN = "UNKNOWN", "Unknown"

    name = models.CharField(max_length=150, help_text="Friendly name for this check")
    check_type = models.CharField(max_length=10, choices=CheckType.choices, default=CheckType.HTTP)

    # HTTP target
    url = models.URLField(max_length=500, blank=True, help_text="For HTTP checks, e.g. https://example.com/health")
    method = models.CharField(max_length=10, default="GET", help_text="HTTP method (GET or HEAD)")
    expected_status = models.CharField(
        max_length=100,
        default="200-399",
        help_text="Accepted status codes: e.g. '200', '200,301', or a range '200-399'",
    )
    expected_substring = models.CharField(
        max_length=255, blank=True,
        help_text="Optional text that must appear in the response body",
    )
    verify_tls = models.BooleanField(default=True, help_text="Verify the TLS certificate for HTTPS checks")

    # TCP target
    host = models.CharField(max_length=255, blank=True, help_text="For TCP checks, hostname or IP")
    port = models.IntegerField(null=True, blank=True, help_text="For TCP checks, port number")

    # Scheduling / behaviour
    timeout_seconds = models.IntegerField(default=10)
    interval_seconds = models.IntegerField(default=60, help_text="How often to run this check")
    enabled = models.BooleanField(default=True)

    # Optional association with a monitored server (purely for grouping)
    server = models.ForeignKey(
        Server, null=True, blank=True, on_delete=models.SET_NULL, related_name="synthetic_checks",
    )

    # Alerting
    alert_on_failure = models.BooleanField(default=True)
    failure_threshold = models.IntegerField(
        default=2, help_text="Consecutive failures before the check is marked DOWN and an alert fires",
    )

    # Live state (maintained by the engine)
    last_status = models.CharField(max_length=10, choices=Status.choices, default=Status.UNKNOWN)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    consecutive_failures = models.IntegerField(default=0)
    consecutive_successes = models.IntegerField(default=0)
    last_state_change_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Synthetic Check"
        verbose_name_plural = "Synthetic Checks"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.get_check_type_display()})"

    @property
    def target(self):
        """Human-readable target string."""
        if self.check_type == self.CheckType.TCP:
            return f"{self.host}:{self.port}"
        return self.url

    def is_due(self, now):
        """Whether this check should run again as of `now`."""
        if not self.enabled:
            return False
        if self.last_checked_at is None:
            return True
        return (now - self.last_checked_at).total_seconds() >= self.interval_seconds


class SyntheticCheckResult(models.Model):
    """A single probe result for a SyntheticCheck."""
    synthetic_check = models.ForeignKey(SyntheticCheck, on_delete=models.CASCADE, related_name="results")
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    success = models.BooleanField(default=False)
    status_code = models.IntegerField(null=True, blank=True)
    response_time_ms = models.FloatField(null=True, blank=True)
    error_message = models.TextField(blank=True)

    class Meta:
        verbose_name = "Synthetic Check Result"
        verbose_name_plural = "Synthetic Check Results"
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["synthetic_check", "-timestamp"]),
        ]

    def __str__(self):
        state = "OK" if self.success else "FAIL"
        return f"{self.synthetic_check.name} {state} @ {self.timestamp}"


class SecurityMonitorConfig(models.Model):
    """Tunable thresholds for the security detection engine (single instance).

    Configurable so teams can tune sensitivity and avoid alert fatigue.
    """
    enabled = models.BooleanField(default=True, help_text="Run security detection")
    alert_enabled = models.BooleanField(default=True, help_text="Send email/Slack alerts for new security events")
    window_minutes = models.IntegerField(default=10, help_text="Look-back window for correlating login events")
    brute_force_ip_threshold = models.IntegerField(
        default=8, help_text="Failed logins from one IP within the window before flagging brute force",
    )
    account_failure_threshold = models.IntegerField(
        default=5, help_text="Failed logins for one account within the window before flagging a spike",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Security Monitor Configuration"
        verbose_name_plural = "Security Monitor Configuration"

    def save(self, *args, **kwargs):
        self.id = 1  # single-instance pattern
        super().save(*args, **kwargs)

    @classmethod
    def get_config(cls):
        config, _ = cls.objects.get_or_create(id=1)
        return config

    def __str__(self):
        return "Security Monitor Configuration"


class SecurityEvent(models.Model):
    """A detected security event/incident (SIEM-style).

    Currently sourced from authentication activity (LoginActivity); designed to
    grow to other sources (file integrity, log signatures).
    """
    class EventType(models.TextChoices):
        BRUTE_FORCE = "BRUTE_FORCE", "Brute-force login attempts"
        LOGIN_FAILURE_SPIKE = "LOGIN_FAILURE_SPIKE", "Login failure spike (account)"
        SUCCESS_AFTER_FAILURES = "SUCCESS_AFTER_FAILURES", "Successful login after repeated failures"
        SSH_BRUTE_FORCE = "SSH_BRUTE_FORCE", "SSH brute-force on a server"

    class Severity(models.TextChoices):
        LOW = "LOW", "Low"
        MEDIUM = "MEDIUM", "Medium"
        HIGH = "HIGH", "High"
        CRITICAL = "CRITICAL", "Critical"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        ACKNOWLEDGED = "acknowledged", "Acknowledged"
        RESOLVED = "resolved", "Resolved"

    event_type = models.CharField(max_length=40, choices=EventType.choices)
    severity = models.CharField(max_length=10, choices=Severity.choices, default=Severity.MEDIUM)
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.OPEN)

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)

    source_ip = models.GenericIPAddressField(null=True, blank=True)
    target_email = models.CharField(max_length=255, blank=True, help_text="Account targeted, if applicable")
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="security_events")
    server = models.ForeignKey(Server, null=True, blank=True, on_delete=models.CASCADE, related_name="security_events",
                               help_text="Monitored server this event relates to (for SSH/host events)")

    event_count = models.IntegerField(default=1, help_text="Number of contributing observations")
    first_seen = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Security Event"
        verbose_name_plural = "Security Events"
        ordering = ["-last_seen"]
        indexes = [
            models.Index(fields=["status", "-last_seen"]),
            models.Index(fields=["event_type", "-last_seen"]),
            models.Index(fields=["source_ip"]),
        ]

    def __str__(self):
        return f"[{self.severity}] {self.get_event_type_display()} ({self.status})"


class BusinessMonitorConfig(models.Model):
    """Single-instance config for Business monitoring, incl. the KPI ingest token.

    Business apps push KPI values to /api/kpi/ingest/ with this bearer token.
    Only a SHA-256 hash of the token is stored (raw shown once on generation).
    """
    ingest_token_hash = models.CharField(max_length=64, blank=True)
    ingest_token_prefix = models.CharField(max_length=12, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Business Monitor Configuration"
        verbose_name_plural = "Business Monitor Configuration"

    def save(self, *args, **kwargs):
        self.id = 1
        super().save(*args, **kwargs)

    @classmethod
    def get_config(cls):
        config, _ = cls.objects.get_or_create(id=1)
        return config

    def generate_token(self):
        """Generate (or rotate) the ingest token. Returns the raw token (shown once)."""
        raw = secrets.token_urlsafe(32)
        self.ingest_token_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        self.ingest_token_prefix = raw[:8]
        self.save(update_fields=["ingest_token_hash", "ingest_token_prefix", "updated_at"])
        return raw

    def verify_token(self, raw_token):
        if not raw_token or not self.ingest_token_hash:
            return False
        return hashlib.sha256(raw_token.encode("utf-8")).hexdigest() == self.ingest_token_hash

    def __str__(self):
        return "Business Monitor Configuration"


class BusinessKPI(models.Model):
    """Definition of a business metric/KPI tracked over time."""
    class Direction(models.TextChoices):
        HIGHER_BETTER = "higher_better", "Higher is better"
        LOWER_BETTER = "lower_better", "Lower is better"

    class Status(models.TextChoices):
        OK = "ok", "OK"
        WARNING = "warning", "Warning"
        CRITICAL = "critical", "Critical"
        UNKNOWN = "unknown", "Unknown"

    name = models.CharField(max_length=150)
    key = models.SlugField(max_length=100, unique=True, help_text="Machine key used when pushing values, e.g. 'signups_per_hour'")
    unit = models.CharField(max_length=30, blank=True, help_text="Display unit, e.g. '/hr', '$', '%'")
    description = models.TextField(blank=True)

    direction = models.CharField(max_length=15, choices=Direction.choices, default=Direction.HIGHER_BETTER)
    warning_threshold = models.FloatField(null=True, blank=True, help_text="Value at which the KPI is in warning")
    critical_threshold = models.FloatField(null=True, blank=True, help_text="Value at which the KPI is critical")

    alert_enabled = models.BooleanField(default=True)
    enabled = models.BooleanField(default=True)

    # Live state
    last_value = models.FloatField(null=True, blank=True)
    last_value_at = models.DateTimeField(null=True, blank=True)
    last_status = models.CharField(max_length=10, choices=Status.choices, default=Status.UNKNOWN)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Business KPI"
        verbose_name_plural = "Business KPIs"
        ordering = ["name"]

    def __str__(self):
        return self.name

    def evaluate(self, value):
        """Return the Status for a given value, honouring direction + thresholds."""
        w, c = self.warning_threshold, self.critical_threshold
        if self.direction == self.Direction.HIGHER_BETTER:
            if c is not None and value <= c:
                return self.Status.CRITICAL
            if w is not None and value <= w:
                return self.Status.WARNING
        else:  # lower is better
            if c is not None and value >= c:
                return self.Status.CRITICAL
            if w is not None and value >= w:
                return self.Status.WARNING
        return self.Status.OK


class BusinessKPIValue(models.Model):
    """A single recorded value for a BusinessKPI (time series)."""
    class Source(models.TextChoices):
        API = "api", "API"
        MANUAL = "manual", "Manual"

    kpi = models.ForeignKey(BusinessKPI, on_delete=models.CASCADE, related_name="values")
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    value = models.FloatField()
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.API)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        verbose_name = "Business KPI Value"
        verbose_name_plural = "Business KPI Values"
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["kpi", "-timestamp"]),
        ]

    def __str__(self):
        return f"{self.kpi.key}={self.value} @ {self.timestamp}"


class Container(models.Model):
    """A container detected on a server by the agent (Docker / Podman / containerd)."""
    server = models.ForeignKey(Server, on_delete=models.CASCADE, related_name="containers")
    container_id = models.CharField(max_length=64, blank=True, help_text="Container ID")
    name = models.CharField(max_length=200)
    runtime = models.CharField(max_length=20, default="docker", help_text="Container runtime: docker, podman, or containerd")
    image = models.CharField(max_length=300, blank=True)
    state = models.CharField(max_length=30, default="running", help_text="running, exited, paused, ...")
    status_text = models.CharField(max_length=200, blank=True, help_text="e.g. 'Up 3 hours'")
    ports = models.CharField(max_length=300, blank=True)
    monitoring_enabled = models.BooleanField(default=False)
    auto_detected = models.BooleanField(default=True)
    last_checked = models.DateTimeField(default=timezone.now)
    inspect_data = models.JSONField(null=True, blank=True, help_text="Compact, sanitized `inspect` summary (config/mounts/networks/env-redacted) from the agent")
    inspect_at = models.DateTimeField(null=True, blank=True, help_text="When the inspect summary was last collected")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Container"
        verbose_name_plural = "Containers"
        unique_together = [["server", "name"]]
        ordering = ["server__name", "name"]
        indexes = [models.Index(fields=["server", "state"])]

    def __str__(self):
        return f"{self.name} on {self.server.name} ({self.state})"


class SSHAuthEvent(models.Model):
    """An SSH authentication event observed on a server (from its auth log)."""
    server = models.ForeignKey(Server, on_delete=models.CASCADE, related_name="ssh_auth_events")
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    source_ip = models.CharField(max_length=64, blank=True)
    username = models.CharField(max_length=150, blank=True)
    success = models.BooleanField(default=False)
    raw = models.CharField(max_length=300, blank=True)

    class Meta:
        verbose_name = "SSH Auth Event"
        verbose_name_plural = "SSH Auth Events"
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["server", "-timestamp"]),
            models.Index(fields=["source_ip"]),
            models.Index(fields=["success", "-timestamp"]),
        ]

    def __str__(self):
        state = "OK" if self.success else "FAIL"
        return f"SSH {state} {self.username}@{self.server.name} from {self.source_ip}"


class AuditLog(models.Model):
    """Security audit trail: impersonation start/exit and denied actions.

    `actor` is always the REAL user (never the impersonated target);
    `impersonated_target` is set when the action happened under impersonation.
    """
    class Result(models.TextChoices):
        ALLOWED = "allowed", "Allowed"
        DENIED = "denied", "Denied"

    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="audit_actions")
    impersonated_target = models.ForeignKey(User, on_delete=models.SET_NULL, null=True,
                                            blank=True, related_name="audit_targeted")
    action = models.CharField(max_length=100, help_text="e.g. impersonate_start, denied")
    resource = models.CharField(max_length=255, blank=True, help_text="path or object")
    method = models.CharField(max_length=10, blank=True)
    result = models.CharField(max_length=10, choices=Result.choices, default=Result.ALLOWED)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        verbose_name = "Audit Log"
        verbose_name_plural = "Audit Logs"
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["-timestamp"]),
            models.Index(fields=["actor", "-timestamp"]),
            models.Index(fields=["result", "-timestamp"]),
        ]

    def __str__(self):
        who = self.actor.username if self.actor else "anonymous"
        as_ = f" as {self.impersonated_target.username}" if self.impersonated_target else ""
        return f"[{self.result}] {who}{as_} {self.action} {self.resource}"
