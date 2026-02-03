# Generated manually for AlertHistory process_context field

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0029_slackalertconfig'),
    ]

    operations = [
        migrations.AddField(
            model_name='alerthistory',
            name='process_context',
            field=models.JSONField(
                blank=True,
                null=True,
                help_text="Top processes at time of alert. Format: {'cpu': [...], 'memory': [...]}"
            ),
        ),
    ]
