from django.core.management.base import BaseCommand
from django.contrib.auth.models import User

from core import permissions as perms
from core.models import Role, UserACL


class Command(BaseCommand):
    help = "Seed the RBAC capabilities + Admin/CEO/Operator roles from the central matrix"

    def handle(self, *args, **options):
        self.stdout.write("Setting up RBAC system...")

        summary = perms.sync_roles()
        self.stdout.write(f"  {summary}")

        admin_role = Role.objects.get(name=perms.ROLE_ADMIN)
        operator_role = Role.objects.get(name=perms.ROLE_OPERATOR)

        # Superusers -> Admin role (they also bypass via is_superuser, but keep
        # their ACL coherent). Other staff with no role -> Operator (safe default).
        for user in User.objects.filter(is_superuser=True):
            acl = UserACL.get_or_create_for_user(user)
            if acl.role_id != admin_role.id:
                acl.role = admin_role
                acl.save(update_fields=["role", "updated_at"])
                self.stdout.write(f"  Assigned Admin to superuser: {user.username}")

        for user in User.objects.filter(is_staff=True, is_superuser=False):
            acl = UserACL.get_or_create_for_user(user)
            if acl.role_id is None:
                acl.role = operator_role
                acl.save(update_fields=["role", "updated_at"])
                self.stdout.write(f"  Assigned Operator to staff user: {user.username}")

        self.stdout.write(self.style.SUCCESS("RBAC setup complete."))
        self.stdout.write(f"  Roles: {list(Role.objects.values_list('name', flat=True))}")
