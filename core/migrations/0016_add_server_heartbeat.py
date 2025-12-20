# Generated manually for heartbeat system

from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0015_add_disk_selection_and_io_thresholds'),
    ]

    operations = [
        migrations.CreateModel(
            name='ServerHeartbeat',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('last_heartbeat', models.DateTimeField(db_index=True, default=django.utils.timezone.now, help_text='Last heartbeat timestamp from agent')),
                ('agent_version', models.CharField(blank=True, help_text='Optional agent version string', max_length=50, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('server', models.ForeignKey(on_delete=models.CASCADE, related_name='heartbeats', to='core.server')),
            ],
            options={
                'verbose_name': 'Server Heartbeat',
                'verbose_name_plural': 'Server Heartbeats',
            },
        ),
        migrations.AddIndex(
            model_name='serverheartbeat',
            index=models.Index(fields=['server', '-last_heartbeat'], name='core_server_server__idx'),
        ),
        migrations.AddIndex(
            model_name='serverheartbeat',
            index=models.Index(fields=['-last_heartbeat'], name='core_server_last_hea_idx'),
        ),
        migrations.AlterUniqueTogether(
            name='serverheartbeat',
            unique_together={('server',)},
        ),
    ]

