"""
Generate (or rotate) the push-agent token for a server.

The raw token is printed exactly once -- it is hashed before storage and cannot
be recovered later. Re-running this command rotates the token (the old one stops
working immediately).

Usage:
    python manage.py create_agent_token <server_id_or_name>
"""

from django.core.management.base import BaseCommand, CommandError

from core.models import Server, AgentCredential


class Command(BaseCommand):
    help = "Generate or rotate the push-agent token for a server (prints it once)."

    def add_arguments(self, parser):
        parser.add_argument(
            "server",
            help="Server id (numeric) or exact server name",
        )

    def handle(self, *args, **options):
        ident = options["server"].strip()

        server = None
        if ident.isdigit():
            server = Server.objects.filter(id=int(ident)).first()
        if server is None:
            server = Server.objects.filter(name=ident).first()
        if server is None:
            raise CommandError(
                f"No server found matching '{ident}' (tried id and exact name)."
            )

        existing = AgentCredential.objects.filter(server=server).first()
        rotated = existing is not None

        cred, raw_token = AgentCredential.generate_for_server(server)

        self.stdout.write("")
        if rotated:
            self.stdout.write(
                self.style.WARNING(
                    f"Rotated agent token for '{server.name}' (id={server.id}). "
                    "The previous token no longer works."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Created agent token for '{server.name}' (id={server.id})."
                )
            )
        self.stdout.write("")
        self.stdout.write("  Save this token now -- it will not be shown again:")
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"    {raw_token}"))
        self.stdout.write("")
        self.stdout.write(f"  (token prefix on record: {cred.token_prefix}...)")
        self.stdout.write("")
