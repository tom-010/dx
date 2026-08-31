"""`manage.py shell_as [-u USERNAME | --last]` — a Django shell acting as one user (tenant).

Both isolation layers are pinned for the whole session: the ORM scope and the database
variable the RLS policies read (session-level, re-applied on reconnect — apps/core/db.py).
`SomeModel.objects.count()` counts that user's rows; another user's rows do not exist.
Cross-tenant work: `manage.py shell_admin`.

Without `-u` a picker shows the most recently used names and Tab-completes usernames. All of
Django's shell options still work (`-i ipython`, `-c "…"`, `--no-startup`).

Not a click command like the others: it subclasses Django's `shell` so the REPL runs in-process
inside the pinned context. Connect to Postgres directly, not through a transaction-pooling
PgBouncer — session-level variables do not survive transaction pooling.
"""

import json
import os
from pathlib import Path

from django.core.management.base import CommandError, CommandParser
from django.core.management.commands import shell
from django_scopes import scope, scopes_disabled

from apps.accounts.models import User
from apps.core.db import pin_session_tenant
from apps.core.history import history_context

MRU_LIMIT = 10


def mru_path() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "dx" / "shell_as.json"


def load_mru() -> list[str]:
    try:
        names = json.loads(mru_path().read_text())
    except OSError, ValueError:
        return []
    return [name for name in names if isinstance(name, str)]


def record_mru(username: str) -> None:
    names = [name for name in load_mru() if name != username]
    names.insert(0, username)
    path = mru_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(names[:MRU_LIMIT]))
    except OSError:
        pass  # a read-only home must not stop the shell


def install_completer(usernames: list[str]) -> None:
    try:
        import readline  # noqa: PLC0415
    except ImportError:  # pragma: no cover - Windows without pyreadline
        return

    def complete(text: str, state: int) -> str | None:
        matches = [name for name in usernames if name.startswith(text)]
        return matches[state] if state < len(matches) else None

    readline.set_completer(complete)
    readline.set_completer_delims("")
    readline.parse_and_bind("tab: complete")


class Command(shell.Command):
    help = "Open a Django shell scoped to a single user (tenant)."

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)
        parser.add_argument("--user", "-u", help="Username; skips the interactive picker.")
        parser.add_argument(
            "--last", action="store_true", help="Reuse the most recently used user."
        )

    def handle(self, **options: object) -> None:
        user = self.pick_user(options)
        record_mru(user.get_username())
        pin_session_tenant(user.pk)
        self.stdout.write(
            self.style.SUCCESS(
                f"\n  Tenant shell — acting as {user.get_username()} (pk={user.pk})\n"
                "  ORM scope active + RLS enforced: only this user's data is visible.\n"
                "  Cross-tenant access: manage.py shell_admin --reason '…'\n"
            )
        )
        # Everything written in here is a version row; say where it came from, or the revision
        # page can only report "no context recorded" (apps/core/history.py).
        with scope(user=user.pk), history_context("command", command="shell_as"):
            super().handle(**options)

    # -- user selection -------------------------------------------------------------------------

    def pick_user(self, options: dict[str, object]) -> User:
        with scopes_disabled():  # accounts_user is a shared table, not tenant-scoped
            usernames = sorted(
                User.objects.filter(is_active=True).values_list("username", flat=True)
            )
        if options.get("user"):
            return self.get_user(str(options["user"]))
        recent = [name for name in load_mru() if name in set(usernames)]
        if options.get("last"):
            if not recent:
                raise CommandError("No previous user recorded.")
            return self.get_user(recent[0])

        if recent:
            self.stdout.write("\nRecent:")
            for index, name in enumerate(recent, 1):
                self.stdout.write(f"  [{index}] {name}")
        install_completer(usernames)
        self.stdout.write(f"\n{len(usernames)} active users. Tab completes.")
        raw = input("User (number or username): ").strip()
        if raw.isdigit() and recent and 1 <= int(raw) <= len(recent):
            return self.get_user(recent[int(raw) - 1])
        if not raw:
            raise CommandError("No user selected.")
        return self.get_user(raw)

    def get_user(self, username: str) -> User:
        with scopes_disabled():
            try:
                return User.objects.get(username=username, is_active=True)
            except User.DoesNotExist:
                raise CommandError(f"No active user named {username!r}.") from None
