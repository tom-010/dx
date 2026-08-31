"""`manage.py shell_admin --reason '…'` — a Django shell with cross-tenant access.

Connects as `app_admin` (BYPASSRLS; DB_ADMIN_USER / DB_ADMIN_PASSWORD, absent from the
app/worker environment on purpose) and disables the ORM scope. Every query crosses tenant
boundaries; `--reason` is mandatory and lands in the audit log (`tenant.admin_access`).
Prefer `manage.py shell_as` for anything that concerns one user.

The default database alias is swapped in place (not a second alias), so ordinary querysets,
`dumpdata` and third-party code work without `.using("admin")`. Subclasses Django's `shell`
like `shell_as` (see there).
"""

import getpass
import socket

import structlog
from django.conf import settings
from django.core.management.base import CommandError, CommandParser
from django.core.management.commands import shell
from django.db import connections
from django_scopes import scopes_disabled

from apps.core.history import history_context
from config.env import django_database, env

audit = structlog.get_logger("tenant.admin_access")


def switch_to_admin_credentials() -> None:
    """Point `DATABASES["default"]` at the admin role. Raises when it is not configured."""
    try:
        credentials = env.database_credentials("admin")
    except ValueError as exc:
        raise CommandError(
            f"{exc}. These credentials are intentionally absent from the web process; "
            "dev: DB_ADMIN_USER=app_admin DB_ADMIN_PASSWORD=app_admin in backend/.env."
        ) from None
    connections.close_all()
    admin = django_database(env.DATABASE_URL, credentials=credentials)
    for key in ("USER", "PASSWORD"):
        # `connections` and the open wrapper share this dict; keep NAME/HOST/PORT as they are
        # (the test suite, for one, points them at its own database).
        settings.DATABASES["default"][key] = admin[key]
        connections["default"].settings_dict[key] = admin[key]
    connections.close_all()


class Command(shell.Command):
    help = "Open a Django shell with cross-tenant access (RLS bypassed, ORM scope off)."

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)
        parser.add_argument(
            "--reason",
            required=True,
            help="Why cross-tenant access is needed. Recorded in the audit log.",
        )

    def handle(self, **options: object) -> None:
        switch_to_admin_credentials()
        reason = str(options["reason"])
        audit.warning(
            "shell_admin_opened",
            os_user=getpass.getuser(),
            host=socket.gethostname(),
            reason=reason,
        )
        self.stdout.write(
            self.style.ERROR(
                "\n" + "=" * 68 + "\n"
                "  ADMIN SHELL — RLS BYPASSED — ALL TENANTS VISIBLE AND WRITABLE\n"
                f"  Reason logged: {reason}\n"
                "  Every query here crosses tenant boundaries. Do not paste results\n"
                "  into shared channels. Prefer `manage.py shell_as` for normal work.\n"
                + "=" * 68
                + "\n"
            )
        )
        with scopes_disabled(), history_context("command", command="shell_admin"):
            super().handle(**options)
