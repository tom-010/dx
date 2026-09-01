#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""

import os
import sys


def main() -> None:
    """Run administrative tasks."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc

    # Every invocation is recorded (apps/core/usage.py), which is what lets `manage.py tui` put
    # the commands you actually use at the top. This is the one place they all pass through, so
    # no command has to remember to do it. Bookkeeping is never the reason a command fails:
    # `record_run` swallows database errors, and so does this.
    try:
        import django

        django.setup()
        from apps.core.usage import record_run

        record_run(sys.argv[1:])
    except Exception:  # noqa: BLE001 - a broken settings module is reported below, not here
        pass

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
