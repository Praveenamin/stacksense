# Seed the role-tailored alert-routing matrix for the three built-in roles.
# Idempotent: only creates cells that don't already exist (never overwrites edits).
from django.db import migrations


# (role_name -> {category -> min_severity|"OFF"}). Mirrors core.alert_routing._DEFAULT_MATRIX.
DEFAULTS = {
    "Admin": {
        "resource": "LOW", "availability": "LOW", "security": "LOW",
        "capacity": "LOW", "business": "LOW",
    },
    "Operator": {
        "resource": "LOW", "availability": "LOW", "security": "LOW",
        "capacity": "LOW", "business": "OFF",
    },
    "CEO": {
        "resource": "OFF", "availability": "CRITICAL", "security": "OFF",
        "capacity": "OFF", "business": "LOW",
    },
}


def seed(apps, schema_editor):
    Role = apps.get_model("core", "Role")
    AlertRoutingRule = apps.get_model("core", "AlertRoutingRule")
    for role_name, cells in DEFAULTS.items():
        role = Role.objects.filter(name=role_name).first()
        if not role:
            continue
        for category, min_sev in cells.items():
            AlertRoutingRule.objects.get_or_create(
                role=role, category=category,
                defaults={"min_severity": min_sev})


def unseed(apps, schema_editor):
    # Reversible: drop the seeded rows (whole table is feature-owned).
    AlertRoutingRule = apps.get_model("core", "AlertRoutingRule")
    AlertRoutingRule.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0058_alertroutingrule'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
