"""Realign the resource SLI/SLO defaults with the truthful calculators.

Phase 1 changed the resource SLIs (CPU/MEMORY/DISK/NETWORK) from "average utilization"
(lower is better, compared with `lte`) to "% of samples at/under a reliability threshold"
(higher is better, compared with `gte`). The seeded global SLOs from migration 0025 still
used the old `lte <threshold>` form, which would read every healthy server as non-compliant.

This migration flips those four global (server=None) SLOs to `gte 95.0` (want >= 95% of
samples under the healthy threshold) -- but only where the row is still the untouched 0025
seed, so any hand-tuned target is preserved. It also refreshes the SLIConfig descriptions to
describe what each SLI now actually measures. UPTIME (availability, gte 99.9), RESPONSE_TIME
(synthetic latency ms, lte 200) and ERROR_RATE (check-failure %, lte 1.0) keep their targets.
"""

from django.db import migrations


# Old 0025 seed for the resource SLOs -> the new (operator, target) under the flipped semantics.
_RESOURCE_OLD_SEED = {
    'CPU': ('lte', 80.0),
    'MEMORY': ('lte', 85.0),
    'DISK': ('lte', 90.0),
    'NETWORK': ('lte', 80.0),
}
_RESOURCE_NEW = ('gte', 95.0)

_SLI_DESCRIPTIONS = {
    'UPTIME': ('percentage', 'Endpoint availability % from synthetic probe success/total'),
    'CPU': ('percentage', '% of samples with CPU at/under the reliability threshold'),
    'MEMORY': ('percentage', '% of samples with memory at/under the reliability threshold'),
    'DISK': ('percentage', '% of samples with primary-disk usage at/under the threshold'),
    'NETWORK': ('percentage', '% of samples with network utilization at/under the threshold'),
    'RESPONSE_TIME': ('average', 'Average synthetic-probe latency (ms) for successful probes'),
    'ERROR_RATE': ('percentage', 'Check-failure % from synthetic probes (100 - availability)'),
}


def realign(apps, schema_editor):
    SLOConfig = apps.get_model('core', 'SLOConfig')
    SLIConfig = apps.get_model('core', 'SLIConfig')
    new_op, new_target = _RESOURCE_NEW

    for metric_type, (old_op, old_target) in _RESOURCE_OLD_SEED.items():
        # Only global defaults still holding the untouched 0025 seed -> flip. Respect overrides.
        SLOConfig.objects.filter(
            server=None, metric_type=metric_type,
            target_operator=old_op, target_value=old_target,
        ).update(target_operator=new_op, target_value=new_target)

    for metric_type, (method, description) in _SLI_DESCRIPTIONS.items():
        SLIConfig.objects.filter(metric_type=metric_type).update(
            calculation_method=method, description=description,
        )


def reverse(apps, schema_editor):
    SLOConfig = apps.get_model('core', 'SLOConfig')
    new_op, new_target = _RESOURCE_NEW
    for metric_type, (old_op, old_target) in _RESOURCE_OLD_SEED.items():
        SLOConfig.objects.filter(
            server=None, metric_type=metric_type,
            target_operator=new_op, target_value=new_target,
        ).update(target_operator=old_op, target_value=old_target)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0067_useracl_email_alerts_enabled'),
    ]

    operations = [
        migrations.RunPython(realign, reverse),
    ]
