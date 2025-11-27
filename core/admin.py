from django import forms
from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django.urls import path, reverse
from django.utils import timezone
from django.utils.safestring import mark_safe
from django.conf import settings
import os
import paramiko
from .models import (
    Server, MonitoredLog, LogEvent, AnalysisRule,
    SystemMetric, Anomaly, MonitoringConfig, Service, AggregatedMetric
)


class ServerForm(forms.ModelForm):
    initial_password = forms.CharField(
        label="Initial Password (for SSH key deployment)",
        widget=forms.PasswordInput(attrs={"placeholder": "Enter password to deploy SSH key"}),
        required=False,
        help_text="Password for initial SSH key deployment. Leave empty if key already deployed."
    )

    class Meta:
        model = Server
        fields = "__all__"


@admin.register(Server)
class ServerAdmin(admin.ModelAdmin):
    form = ServerForm
    list_display = (
        "name",
        "ip_address",
        "username",
        "port",
        "ssh_key_status",
        "monitoring_status",
        "test_connection_button",
    )
    readonly_fields = ("ssh_key_status", "ssh_key_deployed_at")
    fieldsets = (
        ("Connection", {"fields": ("name", "ip_address", "username", "port")}),
        ("SSH Key Management", {
            "fields": ("initial_password", "ssh_key_status", "ssh_key_deployed_at"),
            "description": "Enter password to automatically deploy SSH public key on save."
        }),
    )

    def ssh_key_status(self, obj):
        if not obj.pk:
            return "Pending"
        if obj.ssh_key_deployed:
            return mark_safe("✅ Deployed" + (f" ({obj.ssh_key_deployed_at.strftime("%Y-%m-%d %H:%M")})" if obj.ssh_key_deployed_at else ""))
        return "❌ Not Deployed"
    ssh_key_status.short_description = "SSH Key Status"

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

    def test_connection_button(self, obj):
        if not obj.pk:
            return "N/A"
        url = reverse("admin:test_ssh_connection", args=[obj.pk])
        return mark_safe(f'<a href="{url}" class="button">Test Connection</a>')
    test_connection_button.short_description = "Test"

    def save_model(self, request, obj, form, change):
        initial_password = form.cleaned_data.get("initial_password")
        
        super().save_model(request, obj, form, change)
        
        if not change:
            MonitoringConfig.objects.get_or_create(
                server=obj,
                defaults={
                    "enabled": True,
                    "collection_interval_seconds": 60,
                    "adaptive_collection_enabled": False,
                    "use_adtk": True,
                    "use_isolation_forest": False,
                    "use_llm_explanation": True,
                    "retention_period_days": 30,
                    "aggregation_enabled": True,
                }
            )
            messages.info(request, f"✅ Monitoring configuration created for {obj.name}.")
        
        if initial_password:
            try:
                self._deploy_ssh_key(obj, initial_password)
                obj.ssh_key_deployed = True
                obj.ssh_key_deployed_at = timezone.now()
                obj.save(update_fields=["ssh_key_deployed", "ssh_key_deployed_at"])
                messages.success(request, f"✅ SSH key successfully deployed to {obj.name}.")
            except Exception as e:
                messages.error(request, f"❌ SSH key deployment failed: {str(e)}")
        elif not obj.ssh_key_deployed and not change:
            messages.warning(request, f"⚠ SSH key not deployed. Provide password to deploy automatically.")

    def _deploy_ssh_key(self, server, password):
        private_key_path = getattr(settings, "SSH_PRIVATE_KEY_PATH", "/app/ssh_keys/id_rsa")
        public_key_path = getattr(settings, "SSH_PUBLIC_KEY_PATH", "/app/ssh_keys/id_rsa.pub")
        
        if not os.path.exists(public_key_path):
            raise FileNotFoundError(f"SSH public key not found at {public_key_path}")
        
        with open(public_key_path, "r") as f:
            public_key = f.read().strip()
        
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        try:
            client.connect(
                hostname=server.ip_address,
                port=server.port,
                username=server.username,
                password=password,
                timeout=30
            )
            
            check_cmd = f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && grep -F \"{public_key}\" ~/.ssh/authorized_keys || echo NOT_FOUND"
            stdin, stdout, stderr = client.exec_command(check_cmd)
            key_exists = stdout.read().decode().strip()
            
            if key_exists == "NOT_FOUND":
                add_cmd = f'echo \"{public_key}\" >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys'
                stdin, stdout, stderr = client.exec_command(add_cmd)
                exit_status = stdout.channel.recv_exit_status()
                
                if exit_status != 0:
                    error = stderr.read().decode()
                    raise RuntimeError(f"Failed to add SSH key: {error}")
            
            client.close()
            
            if os.path.exists(private_key_path):
                pkey = paramiko.RSAKey.from_private_key_file(private_key_path)
                test_client = paramiko.SSHClient()
                test_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                test_client.connect(
                    hostname=server.ip_address,
                    port=server.port,
                    username=server.username,
                    pkey=pkey,
                    timeout=10
                )
                test_client.close()
                
        except paramiko.AuthenticationException:
            raise Exception("Authentication failed. Check username and password.")
        except paramiko.SSHException as e:
            raise Exception(f"SSH error: {str(e)}")
        except Exception as e:
            raise Exception(f"Connection error: {str(e)}")
        finally:
            try:
                client.close()
            except:
                pass

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path("<int:server_id>/test-connection/", self.admin_site.admin_view(self.test_connection_view), name="test_ssh_connection"),
        ]
        return custom + urls

    def test_connection_view(self, request, server_id):
        from django.shortcuts import get_object_or_404
        server = get_object_or_404(Server, pk=server_id)
        
        try:
            private_key_path = getattr(settings, "SSH_PRIVATE_KEY_PATH", "/app/ssh_keys/id_rsa")
            if not os.path.exists(private_key_path):
                messages.error(request, f"SSH private key not found at {private_key_path}")
                return HttpResponseRedirect(reverse("admin:core_server_changelist"))
            
            pkey = paramiko.RSAKey.from_private_key_file(private_key_path)
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                hostname=server.ip_address,
                port=server.port,
                username=server.username,
                pkey=pkey,
                timeout=10
            )
            
            stdin, stdout, stderr = client.exec_command("echo \"Connection successful\"")
            output = stdout.read().decode().strip()
            client.close()
            
            messages.success(request, f"✅ SSH connection successful! Server responded: {output}")
        except Exception as e:
            messages.error(request, f"❌ Connection test failed: {str(e)}")
        
        return HttpResponseRedirect(reverse("admin:core_server_changelist"))


@admin.register(MonitoringConfig)
class MonitoringConfigAdmin(admin.ModelAdmin):
    list_display = ("server", "enabled", "collection_interval_seconds", "use_adtk", "use_llm_explanation")
    list_filter = ("enabled", "use_adtk", "adaptive_collection_enabled", "aggregation_enabled")


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


# Override admin index to redirect to monitoring
from django.shortcuts import redirect
from django.contrib.auth.decorators import login_required

original_index = admin.site.index

@login_required
def custom_admin_index(request, extra_context=None):
    # Redirect authenticated users to monitoring
    return redirect("/monitoring/")

admin.site.index = custom_admin_index
