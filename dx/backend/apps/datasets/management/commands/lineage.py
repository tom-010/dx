from pathlib import Path

import djclick as click
import structlog
from django.db import transaction
from django.utils import timezone
from pydantic import BaseModel as PydanticModel

from apps.accounts.models import User
from apps.core import lineage
from apps.core.db import tenant_context
from apps.core.history import history_context
from apps.core.models import OwnedModel
from apps.core.revisions import context_descriptions, context_sources
from apps.datasets import models

log = structlog.get_logger(__name__)


@history_context("concat-names")
def idea2() -> tuple[models.ModelA, list[models.ModelA | models.ModelB]]:
    """The primitive, spelled out: rows first, then `record_derivation()` as a second statement.

    Every write still has to answer for its lineage — `sources=None` (no block to defer to) and
    `operation=None` (the decorator's label applies) — which is the point: the old two-step is
    still possible, it just cannot happen by accident any more.
    """
    a1 = models.ModelA.create(name="model a1", operation=None, sources=None)
    a2 = models.ModelA.create(name="model a2", operation=None, sources=None)
    b1 = models.ModelB.create(name="model b1", operation=None, sources=None)
    b2 = models.ModelB.create(name="model b2", operation=None, sources=None)
    derived = models.ModelA.create(
        name=", ".join(source.name for source in (a1, a2, b1, b2)), operation=None, sources=None
    )
    lineage.record_derivation(derived, sources=[a1, a2, b1, b2])
    return derived, [a1, a2, b1, b2]


class Rename(PydanticModel):
    """A PATCH body, as the API would send one — for the `set_payload_partial()` path below."""

    name: str


def idea3() -> tuple[models.ModelA, list[models.ModelA | models.ModelB], list[OwnedModel]]:
    """Every way to write a row, each carrying its lineage and its step.

    idea2 needed three places for one derivation — a decorator for the label, `objects.create()`
    for the row, `record_derivation()` for the edges. Here every write path takes the same two
    keywords, `operation=` and `sources=`, and both land in the row's own transaction. Neither
    has a default: `[]` says "from nothing", `None` says "whatever the enclosing block says", and
    leaving one out is a TypeError. Returns the derived row, its inputs, and the other rows
    worth printing.
    """
    # --- creating -------------------------------------------------------------------------------

    # 1. `Model.create(...)`: one statement for the row, its sources and its step. The inputs are
    #    built from nothing, and say so — `sources=[]` — rather than leaving it out.
    a1 = models.ModelA.create(name="model a1", operation="idea3: inputs", sources=[])
    a2 = models.ModelA.create(name="model a2", operation="idea3: inputs", sources=[])
    b1 = models.ModelB.create(name="model b1", operation="idea3: inputs", sources=[])
    b2 = models.ModelB.create(name="model b2", operation="idea3: inputs", sources=[])
    inputs: list[models.ModelA | models.ModelB] = [a1, a2, b1, b2]

    derived = models.ModelA.create(
        name=", ".join(source.name for source in inputs),
        operation="idea3: concat names",
        sources=inputs,
        operation_description="joined the four input names with ', ' in input order",
    )

    # 2. Construct, then `save(...)`: the same two keywords, on the save.
    copy = models.ModelA(name=f"copy of {derived.name}")
    copy.save(operation="idea3: copy via save()", sources=[derived])

    # 3. Deferring to the blocks: `operation=None` / `sources=None` mean "whatever the enclosing
    #    `deriving()` / `history_context()` say". Still spelled out — `None` is an answer, an
    #    omitted keyword is a TypeError. (`objects.create()` is refused outright: a manager's
    #    create() cannot carry the two keywords, so it cannot be explicit.)
    with history_context("idea3: deferred to the blocks"), lineage.deriving(derived):
        blocked = models.ModelB.create(
            name=f"summary of {derived.name}", operation=None, sources=None
        )

    # --- updating -------------------------------------------------------------------------------

    # 4. Edit and `save(...)`: a new version, edges against *that* version, its own step. An edit
    #    that derives from nothing new says so, and just carries the label.
    b2.name = "model b2 (renamed)"
    b2.save(operation="idea3: rename b2", sources=[])
    derived.name = ", ".join(source.name for source in inputs)
    derived.save(
        operation="idea3: rebuild after rename",
        sources=inputs,
        operation_description="b2 was renamed after the first build; recomputed from all four",
    )

    # 5. A partial save (`update_fields`): same keywords.
    copy.name = f"copy of {derived.name}"
    copy.save(update_fields=["name"], operation="idea3: partial rebuild", sources=[derived])

    # 6. The API's PATCH idiom: `set_payload_partial()` applies what the client sent, `save()`
    #    records where it came from. PUT is `set_payload()` and otherwise identical.
    blocked.set_payload_partial(Rename(name=f"summary of {derived.name} (patched)"))
    blocked.save(operation="idea3: PATCH", sources=[derived])

    # 7. A bulk `.update()`: versioned by the trigger and labelled by the block — but no `save()`
    #    runs, so there is nowhere for `sources=` to go. The one write path lineage cannot ride
    #    on; better to know than to assume.
    with history_context("idea3: bulk rename"):
        models.ModelB.objects.filter(pk=b1.pk).update(name="model b1 (bulk)")

    # 8. Delete and restore are versioned writes too. `delete()` is soft and records no edges
    #    even inside a `deriving()` block — retiring a row is not deriving it from anything.
    #    Putting it back is an ordinary save, with the next version number.
    b1.refresh_from_db()
    b1.delete()
    b1.deleted_at = None
    b1.save(operation="idea3: restore b1", sources=[])

    return derived, inputs, [copy, blocked]


def print_lineage(obj: OwnedModel, stack: bool = False) -> None:
    """What `obj` was built from, grouped by the run that recorded it.

    An edge takes its `pgh_context` from the *target's version*, not from the run that called
    `record_derivation` — so a group is "what this version of `obj` was built from", and its
    label ("concat names") is the operation that wrote that version, with its description
    underneath when the write gave one. Recording an edge later, from another context, still
    files it under the version's own run.

    A source is a *version*, so a line prints the name the source had when it was consumed;
    "stale" means the live row has moved on and a rebuild would come out differently.

    Each group also names the line that recorded it (`Lineage.stack`, captured at
    `record_derivation`); `stack=True` prints the whole call stack instead of just the caller.
    """
    print(f"{obj} (v{obj.version})")
    edges = list(lineage.all_sources_of(obj))
    contexts = {edge.pgh_context for edge in edges if edge.pgh_context}
    producers = context_sources(contexts)
    descriptions = context_descriptions(contexts)

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
        if context_id is not None and context_id in descriptions:
            print(f"    “{descriptions[context_id]}”")
        caller = members[0].caller
        for frame in members[0].stack if stack else ([caller] if caller else []):
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


def print_history(obj: OwnedModel) -> None:
    """Every state the row has been in, oldest first — the version chain the edges point into."""
    print(f"{obj} history:")
    for version in obj.history():
        when = timezone.localtime(version.at).strftime("%Y-%m-%d %H:%M:%S")
        state = " [deleted]" if version.deleted else ""
        print(f"  v{version.version} {version.to_object()} at {when}{state}")


@click.command()
def command() -> None:
    user = User.objects.get(username="admin")
    with tenant_context(user.pk), transaction.atomic():
        # idea1()
        derived, sources = idea2()
        b2 = sources[-1]
        print_lineage(derived)

        print("\n=== idea3: every write path, with lineage ===")
        derived3, inputs3, others3 = idea3()
        for row in (derived3, *others3):
            print()
            print_lineage(row)
        print()
        print_history(inputs3[2])  # b1: created, bulk-renamed, deleted, restored
        print("\n=== idea2 continued ===")

        # Versioning: change a source *after* it was consumed. The FK would follow the rename;
        # the edge does not — it names the version that was read, so the line below keeps the
        # old name and turns stale, which is what `lineage.stale_derivations(b2)` finds.
        with history_context("rename-b2"):
            b2.name = "model b2 (renamed)"
            b2.save(operation=None, sources=[])

        print()
        print_history(b2)
        print()
        print_lineage(derived)

        # A second group needs a second *version of the target*: an edge inherits the context of
        # the version it feeds, so recording one without rewriting `derived` would file it under
        # the original run. Rebuilding the row is what a stale source actually calls for anyway.
        with history_context("rebuild-after-rename"):
            derived.name = ", ".join(source.name for source in sources)
            derived.save(operation=None, sources=None)  # the edges come from the next line
            lineage.record_derivation(derived, sources=sources)

        print()
        print_lineage(derived)
