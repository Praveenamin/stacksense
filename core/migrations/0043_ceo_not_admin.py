"""
Re-sync built-in role privileges to the central matrix after narrowing CEO:
CEO no longer has user/role administration (manage_users / manage_roles).
Idempotent — sets each built-in role's privileges to exactly its matrix set.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    from core import permissions as P

    Privilege = apps.get_model("core", "Privilege")
    Role = apps.get_model("core", "Role")
    RolePrivilege = apps.get_model("core", "RolePrivilege")

    for name, caps in P.ROLE_CAPABILITIES.items():
        try:
            role = Role.objects.get(name=name)
        except Role.DoesNotExist:
            continue
        wanted = set(caps)
        for key in wanted:
            priv, _ = Privilege.objects.get_or_create(
                key=key, defaults={"label": P.CAPABILITY_LABELS.get(key, key)})
            RolePrivilege.objects.get_or_create(role=role, privilege=priv)
        RolePrivilege.objects.filter(role=role).exclude(
            privilege__key__in=wanted).delete()


def backwards(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("core", "0042_rbac_cleanup")]
    operations = [migrations.RunPython(forwards, backwards)]
