from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from core.models import Privilege, Role, RolePrivilege, UserACL


class Command(BaseCommand):
    help = 'Set up the RBAC system with default privileges and roles'

    def handle(self, *args, **options):
        self.stdout.write('Setting up RBAC system...')

        # Create default privileges
        privileges_data = [
            ('view_dashboard', 'View Dashboard'),
            ('view_servers', 'View Servers'),
            ('add_server', 'Add Server'),
            ('edit_server', 'Edit Server'),
            ('delete_server', 'Delete Server'),
            ('view_metrics', 'View Metrics'),
            ('view_anomalies', 'View Anomalies'),
            ('resolve_anomalies', 'Resolve Anomalies'),
            ('configure_alerts', 'Configure Alerts'),
            ('manage_users', 'Manage Users'),
            ('manage_roles', 'Manage Roles'),
            ('suspend_monitoring', 'Suspend Monitoring'),
            ('configure_thresholds', 'Configure Thresholds'),
            ('view_alert_history', 'View Alert History'),
        ]

        privileges = {}
        for key, label in privileges_data:
            privilege, created = Privilege.objects.get_or_create(
                key=key,
                defaults={'label': label}
            )
            privileges[key] = privilege
            if created:
                self.stdout.write(f'  Created privilege: {key}')

        # Create Root Admin role (protected)
        root_admin_role, created = Role.objects.get_or_create(
            name='Root Admin',
            defaults={
                'description': 'Full system administrator with all privileges',
                'is_protected': True
            }
        )
        if created:
            self.stdout.write('  Created Root Admin role')

        # Assign all privileges to Root Admin
        for privilege in privileges.values():
            RolePrivilege.objects.get_or_create(
                role=root_admin_role,
                privilege=privilege
            )

        # Create Viewer role (default for new users)
        viewer_role, created = Role.objects.get_or_create(
            name='Viewer',
            defaults={
                'description': 'Read-only access to monitoring data',
                'is_protected': False
            }
        )
        if created:
            self.stdout.write('  Created Viewer role')

        # Assign viewer privileges
        viewer_privileges = [
            'view_dashboard',
            'view_servers',
            'view_metrics',
            'view_anomalies',
            'view_alert_history'
        ]
        for priv_key in viewer_privileges:
            RolePrivilege.objects.get_or_create(
                role=viewer_role,
                privilege=privileges[priv_key]
            )

        # Create Operator role
        operator_role, created = Role.objects.get_or_create(
            name='Operator',
            defaults={
                'description': 'Can manage servers and resolve issues',
                'is_protected': False
            }
        )
        if created:
            self.stdout.write('  Created Operator role')

        # Assign operator privileges
        operator_privileges = [
            'view_dashboard',
            'view_servers',
            'add_server',
            'edit_server',
            'view_metrics',
            'view_anomalies',
            'resolve_anomalies',
            'configure_alerts',
            'suspend_monitoring',
            'configure_thresholds',
            'view_alert_history'
        ]
        for priv_key in operator_privileges:
            RolePrivilege.objects.get_or_create(
                role=operator_role,
                privilege=privileges[priv_key]
            )

        # Assign Root Admin role to superuser(s)
        superusers = User.objects.filter(is_superuser=True)
        for user in superusers:
            acl, created = UserACL.objects.get_or_create(user=user)
            if not acl.role or acl.role != root_admin_role:
                acl.role = root_admin_role
                acl.save()
                self.stdout.write(f'  Assigned Root Admin role to superuser: {user.username}')

        # Assign Viewer role to existing non-superuser staff (if no role assigned)
        staff_users = User.objects.filter(is_staff=True, is_superuser=False)
        for user in staff_users:
            acl, created = UserACL.objects.get_or_create(user=user)
            if not acl.role:
                acl.role = viewer_role
                acl.save()
                self.stdout.write(f'  Assigned Viewer role to staff user: {user.username}')

        self.stdout.write(self.style.SUCCESS('RBAC system setup completed!'))
        self.stdout.write(f'  Created {len(privileges)} privileges')
        self.stdout.write(f'  Created {Role.objects.count()} roles')
        self.stdout.write(f'  Assigned roles to {UserACL.objects.filter(role__isnull=False).count()} users')
