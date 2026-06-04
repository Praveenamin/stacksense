"""
Reconcile RBAC roles/privileges with the central matrix in core.permissions:
- seed the 10 canonical capabilities + the Admin/CEO/Operator roles (protected),
- migrate any users still on legacy roles (Root Admin / Viewer) to Admin/Operator,
- delete the legacy roles and the orphaned old privilege rows.

Runs on every deploy so fresh installs are seeded and existing DBs are cleaned.
"""
from django.db import migrations

LEGACY_ROLES = ["Root Admin", "Viewer"]


def forwards(apps, schema_editor):
    from core import permissions as P

    Privilege = apps.get_model("core", "Privilege")
    Role = apps.get_model("core", "Role")
    RolePrivilege = apps.get_model("core", "RolePrivilege")
    UserACL = apps.get_model("core", "UserACL")

    # 1) Canonical capabilities.
    for key in P.ALL_CAPABILITIES:
        Privilege.objects.get_or_create(
            key=key, defaults={"label": P.CAPABILITY_LABELS.get(key, key)})

    # 2) Built-in roles (protected) with their capability sets.
    for name, caps in P.ROLE_CAPABILITIES.items():
        role, _ = Role.objects.get_or_create(
            name=name,
            defaults={"is_protected": True,
                      "description": f"{name} role (managed by RBAC matrix)"})
        if not role.is_protected:
            role.is_protected = True
            role.save()
        wanted = set(caps)
        for key in wanted:
            priv = Privilege.objects.get(key=key)
            RolePrivilege.objects.get_or_create(role=role, privilege=priv)
        RolePrivilege.objects.filter(role=role).exclude(
            privilege__key__in=wanted).delete()

    # 3) Migrate users off legacy roles, then remove the legacy roles.
    try:
        admin_role = Role.objects.get(name=P.ROLE_ADMIN)
        operator_role = Role.objects.get(name=P.ROLE_OPERATOR)
    except Role.DoesNotExist:
        admin_role = operator_role = None

    for legacy in Role.objects.filter(name__in=LEGACY_ROLES):
        if admin_role and operator_role:
            for acl in UserACL.objects.filter(role=legacy):
                acl.role = admin_role if acl.user.is_superuser else operator_role
                acl.save()
        if not UserACL.objects.filter(role=legacy).exists():
            legacy.delete()

    # 4) Drop orphaned old privilege rows (cascades RolePrivilege).
    Privilege.objects.exclude(key__in=P.ALL_CAPABILITIES).delete()


def backwards(apps, schema_editor):
    # One-way reconciliation; nothing to restore.
    pass


class Migration(migrations.Migration):
    dependencies = [("core", "0041_auditlog")]
    operations = [migrations.RunPython(forwards, backwards)]
