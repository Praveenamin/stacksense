# Generated migration for I/O rate fields

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0004_add_server_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='systemmetric',
            name='disk_io_read',
            field=models.BigIntegerField(blank=True, help_text='Disk read rate (bytes/second)', null=True),
        ),
        migrations.AddField(
            model_name='systemmetric',
            name='disk_io_write',
            field=models.BigIntegerField(blank=True, help_text='Disk write rate (bytes/second)', null=True),
        ),
        migrations.AddField(
            model_name='systemmetric',
            name='net_io_recv',
            field=models.BigIntegerField(blank=True, help_text='Network received rate (bytes/second)', null=True),
        ),
        migrations.AddField(
            model_name='systemmetric',
            name='net_io_sent',
            field=models.BigIntegerField(blank=True, help_text='Network sent rate (bytes/second)', null=True),
        ),
    ]
