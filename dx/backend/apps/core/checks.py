"""System checks that keep the multitenancy invariants (CLAUDE.md "Multitenancy") true as the
code base grows. Registered in `apps.core.apps.CoreConfig.ready()`.

- tenant.E001 — a concrete model in a tenant app does not inherit `BaseModel` (and is not in
  `SHARED_MODELS`). The highest-leverage check here: it makes the invariant survive new
  developers and new models.
- tenant.E002 — an auto-created many-to-many through table on an owned model: no `owner`
  column, so no row-level security can protect it.
- tenant.E003 — (`manage.py check --database default`) a table is missing its RLS policy.
  Skipped while migrations are pending: `migrate` runs the database checks before it applies
  anything, and the post-migrate `rls_sync --check` is the gate for that case.
- tenant.E004 — a generated event table (apps/core/history.py) does not mirror the owner
  column, so the tenant policy cannot key on it and one tenant could read another's history.
"""

from collections.abc import Iterable, Sequence

import pghistory.models
from django.apps import AppConfig, apps
from django.conf import settings
from django.core.checks import CheckMessage, Error, Tags, register
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models import Model

from apps.core.models import OWNER_COLUMN, BaseModel


def tenant_app_labels() -> set[str]:
    return {
        config.label for config in apps.get_app_configs() if config.name in settings.TENANT_APPS
    }


def tenant_model_errors(models: Iterable[type[Model]]) -> list[CheckMessage]:
    errors: list[CheckMessage] = []
    shared = set(settings.SHARED_MODELS)
    tenant_labels = tenant_app_labels()

    for model in models:
        label = model._meta.label
        if model._meta.abstract or model._meta.proxy:
            continue
        if model._meta.app_label not in tenant_labels or label in shared:
            continue

        if issubclass(model, pghistory.models.Event):
            # Event tables are generated into the tracked model's app and cannot inherit
            # BaseModel. They mirror the tracked columns, so they carry `owner_id` and get the
            # same policy (rls.isolated_models) — unless someone excluded the column, which is
            # what E004 is for.
            if not any(field.attname == OWNER_COLUMN for field in model._meta.fields):
                errors.append(
                    Error(
                        f"{label} mirrors an owned model but has no {OWNER_COLUMN} column, "
                        "so no row-level security policy can protect it: every tenant would "
                        "read every tenant's history.",
                        hint="Do not exclude the owner field from @tracked.",
                        obj=model,
                        id="tenant.E004",
                    )
                )
            continue

        if not issubclass(model, BaseModel):
            errors.append(
                Error(
                    f"{label} is in a tenant app but does not inherit BaseModel.",
                    hint=(
                        "Inherit apps.core.models.BaseModel, or add the label to "
                        "settings.SHARED_MODELS after a security review."
                    ),
                    obj=model,
                    id="tenant.E001",
                )
            )

        for field in model._meta.many_to_many:
            through = field.remote_field.through
            if isinstance(through, type) and through._meta.auto_created:
                errors.append(
                    Error(
                        f"{label}.{field.name} uses an auto-created M2M through table, which "
                        "has no owner column and cannot be protected by row-level security.",
                        hint="Declare an explicit through= model inheriting BaseModel.",
                        obj=model,
                        id="tenant.E002",
                    )
                )
    return errors


@register(Tags.models)
def check_tenant_models(
    app_configs: Sequence[AppConfig] | None, **kwargs: object
) -> list[CheckMessage]:
    return tenant_model_errors(apps.get_models())


@register(Tags.database)
def check_rls_applied(
    app_configs: Sequence[AppConfig] | None, databases: Sequence[str] | None, **kwargs: object
) -> list[CheckMessage]:
    """`manage.py check --database default`: every owned table carries its policy."""
    from apps.core import rls  # noqa: PLC0415 - avoid importing the database layer at startup

    if not databases or "default" not in databases:
        return []
    executor = MigrationExecutor(connection)
    if executor.migration_plan(executor.loader.graph.leaf_nodes()):
        return []  # mid-deploy: `rls_sync --check` after `migrate` is the gate
    problems = rls.verify()
    if not problems:
        return []
    return [
        Error(
            "Row-level security drift:\n  " + "\n  ".join(problems),
            hint="Run `manage.py rls_sync` as the table owner (./scripts/migrate.sh).",
            id="tenant.E003",
        )
    ]
