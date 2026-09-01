from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "apps.core"

    def ready(self) -> None:
        # System checks (tenant.E00x) and the connection_created receiver of the shell context.
        from apps.core import checks, db  # noqa: F401, PLC0415
