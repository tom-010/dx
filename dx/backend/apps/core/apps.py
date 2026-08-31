from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "apps.core"

    def ready(self) -> None:
        # System checks (tenant.E00x) and the connection_created receiver of the shell context.
        from apps.core import checks, db  # noqa: F401, PLC0415
        from apps.core.admin import register_event_admins  # noqa: PLC0415

        # After every app has registered its own models: which histories are browsable follows
        # from which models the admin shows. `apps.core` is listed last of the shared apps and
        # the feature apps register on import of their `admin` module, which the admin
        # autodiscovery has already done by the time app configs are ready.
        register_event_admins()
