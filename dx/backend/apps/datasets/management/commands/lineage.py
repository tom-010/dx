from pathlib import Path

import djclick as click
import structlog
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.core import lineage
from apps.core.db import tenant_context
from apps.core.history import history_context
from apps.core.models import BaseModel
from apps.core.revisions import context_sources
from apps.datasets import models

log = structlog.get_logger(__name__)


@history_context("concat-names")
def idea2() -> tuple[models.ModelA, list[models.ModelA | models.ModelB]]:
    a1 = models.ModelA.objects.create(name="model a1")
    a2 = models.ModelA.objects.create(name="model a2")
    b1 = models.ModelB.objects.create(name="model b1")
    b2 = models.ModelB.objects.create(name="model b2")
    derived = models.ModelA.objects.create(
        name=", ".join(source.name for source in (a1, a2, b1, b2))
    )
    lineage.record_derivation(derived, sources=[a1, a2, b1, b2])
    return derived, [a1, a2, b1, b2]


def print_lineage(obj: BaseModel, stack: bool = False) -> None:
    """What `obj` was built from, grouped by the run that recorded it.

    An edge takes its `pgh_context` from the *target's version*, not from the run that called
    `record_derivation` — so a group is "what this version of `obj` was built from", and its
    label ("concat-names") is the step that wrote that version. Recording an edge later, from
    another context, still files it under the version's own run.

    A source is a *version*, so a line prints the name the source had when it was consumed;
    "stale" means the live row has moved on and a rebuild would come out differently.

    Each group also names the line that recorded it (`Lineage.stack`, captured at
    `record_derivation`); `stack=True` prints the whole call stack instead of just the caller.
    """
    print(f"{obj} (v{obj.version})")
    edges = list(lineage.all_sources_of(obj))
    producers = context_sources({edge.pgh_context for edge in edges if edge.pgh_context})

    groups: dict[object, list[lineage.Lineage]] = {}
    for index, edge in enumerate(edges):
        # An edge written without a context (a shell, a job that forgot to open one) is its own
        # group: those are genuinely unrelated, and folding them together would invent a step
        # that never happened. `revisions.group_by_context` keys orphans the same way.
        key = edge.pgh_context if edge.pgh_context is not None else ("orphan", index)
        groups.setdefault(key, []).append(edge)

    for members in groups.values():
        context_id = members[0].pgh_context
        producer = "unknown" if context_id is None else producers.get(context_id, "unknown")
        when = timezone.localtime(members[0].created).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  {producer} · {when}")
        frames = members[0].stack
        for frame in frames if stack else frames[-1:]:
            print(
                f"    at {Path(frame.file).name}:{frame.line} in {frame.func}() "
                f"@{members[0].release} — {frame.code}"
            )
        for edge in members:
            source = lineage.source_versions([edge])[0]
            state = "current" if source.is_current() else "stale"
            print(
                f"    <- {source.model.__name__} v{source.version} {source.to_object()} [{state}]"
            )


def print_history(obj: BaseModel) -> None:
    """Every state the row has been in, oldest first — the version chain the edges point into."""
    print(f"{obj} history:")
    for version in obj.history():
        when = timezone.localtime(version.at).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  v{version.version} {version.to_object()} at {when}")


@click.command()
def command() -> None:
    user = User.objects.get(username="admin")
    with tenant_context(user.pk), transaction.atomic():
        # idea1()
        derived, sources = idea2()
        b2 = sources[-1]
        print_lineage(derived)

        # Versioning: change a source *after* it was consumed. The FK would follow the rename;
        # the edge does not — it names the version that was read, so the line below keeps the
        # old name and turns stale, which is what `lineage.stale_derivations(b2)` finds.
        with history_context("rename-b2"):
            b2.name = "model b2 (renamed)"
            b2.save()

        print()
        print_history(b2)
        print()
        print_lineage(derived)

        # A second group needs a second *version of the target*: an edge inherits the context of
        # the version it feeds, so recording one without rewriting `derived` would file it under
        # the original run. Rebuilding the row is what a stale source actually calls for anyway.
        with history_context("rebuild-after-rename"):
            derived.name = ", ".join(source.name for source in sources)
            derived.save()
            lineage.record_derivation(derived, sources=sources)

        print()
        print_lineage(derived)
