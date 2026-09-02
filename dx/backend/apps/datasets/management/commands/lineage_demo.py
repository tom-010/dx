"""`manage.py lineage_demo` — build a lineage graph worth exploring, out of `ModelA` and `ModelB`.

`lineage.py` in this directory is the one-derivation sketch. This is the same idea at the size
where the *shapes* start to matter, because a chain is the only shape a small example ever has
and it is the least interesting one:

    pipeline     four generations, so depth is visible — and only the *first* derivation goes
                 stale when the head changes, which is the thing people assume wrong
    merge        five sources into one row: fan-in, one target with many parents
    split        one row into three: fan-out, and the three are siblings of each other
    diamond      one source, two projections, one result built from both: co-parents, and the
                 shape a plain parent pointer cannot express at all
    feedback     a → b, then b back into a's next version: a cycle between *rows* that is not a
                 cycle between versions, because an edge names a version and versions only go
                 forward
    rebuild      derive, change the source, rebuild: two groups on one target, the older one
                 still pinned to what it actually consumed
    erased       derive, then soft-delete the source: the edge still resolves, which is why
                 nothing is ever hard-deleted
    hub          twelve rows built from one: the "used to build" side at a size that pages
    churn        one row edited eight times, every edit its own run, so the history reads as a
                 list of who changed what rather than as one undifferentiated chain
    restore      written, edited, retired, brought back: a delete is a version like any other,
                 and so is undoing it — the chain never loses a state
    moving       a source revised twice with the report rebuilt after each: one target with
                 three groups of edges, pinned to three different versions of the same row

Everything lands in one user's tenant and each shape opens its own `history_context`, so the
explorer (`/explorer/`) groups the edges by the step that made them.

    uv run python manage.py lineage_demo                # add a graph for `admin`
    uv run python manage.py lineage_demo -u alice --clean

Re-running adds another, disjoint graph; `--clean` retires the previous rows first (soft
delete — they stay visible in the explorer as deleted, because that is what soft delete means).
"""

from dataclasses import dataclass

import djclick as click
import structlog
from django.conf import settings
from django.db import transaction
from django.urls import reverse
from rich.console import Console
from rich.table import Table

from apps.accounts.models import User
from apps.core import lineage
from apps.core.db import tenant_context
from apps.core.history import history_context
from apps.core.models import OwnedModel
from apps.datasets.models import ModelA, ModelB

console = Console()
log = structlog.get_logger(__name__)

#: How many rows the fan-in, fan-out and hub shapes build. Big enough that the page has to say
#: something about them, small enough to read.
PARTS = 5
PIECES = 3
CONSUMERS = 12
#: Edits the `churn` shape makes, so its row ends up at version EDITS + 1.
EDITS = 7


@dataclass(frozen=True)
class Scenario:
    """One shape, and the row to open in the explorer to see it."""

    name: str
    shape: str
    root: OwnedModel


def a(name: str) -> ModelA:
    return ModelA.objects.create(name=name)


def b(name: str) -> ModelB:
    return ModelB.objects.create(name=name)


# --- the shapes ----------------------------------------------------------------------------------


@history_context("pipeline")
def pipeline() -> Scenario:
    """Four generations, then a change at the head.

    The change is the point: `stale_derivations(raw)` finds exactly one edge — the parsed rows.
    Staleness does not propagate. `normalised` was built from a version of `parsed` that has not
    moved, so it is still perfectly up to date with respect to what it consumed, and only
    becomes stale once `parsed` is actually rebuilt. A work list is one level deep at a time.
    """
    raw = b("pipeline: raw upload")
    parsed = a("pipeline: parsed rows")
    lineage.record_derivation(parsed, sources=[raw])

    normalised = a("pipeline: normalised rows")
    lineage.record_derivation(normalised, sources=[parsed])

    summary = a("pipeline: summary")
    lineage.record_derivation(summary, sources=[normalised])

    raw.name = "pipeline: raw upload (re-uploaded)"
    raw.save()
    return Scenario("pipeline", f"chain of 4; {lineage.stale_derivations(raw).count()} stale", raw)


def merge() -> Scenario:
    """Fan-in: many parents, each pinned to the version that was read.

    Two of the five are edited afterwards, so the report's sources are a mix of current and
    stale. That mix is the normal state of a real graph — "is this out of date" is per edge, not
    per row, and a page that only ever showed all-fresh or all-stale would be hiding it.
    """
    with history_context("merge"):
        parts = [b(f"merge: part {index}") for index in range(1, PARTS + 1)]
        merged = a("merge: merged report")
        lineage.record_derivation(merged, sources=parts)

    for part in parts[:2]:
        with history_context(f"merge: corrected {part.name.rsplit(' ', 1)[-1]}"):
            part.name = f"{part.name} (corrected)"
            part.save()

    stale = sum(1 for version in merged.sources() if not version.is_current())
    return Scenario("merge", f"{PARTS} sources into 1, {stale} stale", merged)


@history_context("split")
def split() -> Scenario:
    """Fan-out: three rows off one parent, which makes them siblings — a shape only reachable by
    going up and back down again, which is why `lineage.graph()` walks edges in both directions."""
    whole = a("split: whole document")
    for index in range(1, PIECES + 1):
        piece = b(f"split: section {index}")
        lineage.record_derivation(piece, sources=[whole])
    return Scenario("split", f"1 source into {PIECES} siblings", whole)


@history_context("diamond")
def diamond() -> Scenario:
    """One source, two projections, one result built from both: `joined` has two parents that
    share a grandparent. A `parent_id` column cannot say this; a graph of edges can."""
    source = a("diamond: source table")
    left = b("diamond: left projection")
    right = b("diamond: right projection")
    lineage.record_derivation(left, sources=[source])
    lineage.record_derivation(right, sources=[source])

    joined = a("diamond: joined result")
    lineage.record_derivation(joined, sources=[left, right])
    return Scenario("diamond", "two paths from one source", joined)


def feedback() -> Scenario:
    """A cycle between rows that is not a cycle in the graph.

    `scores` was built from version 1 of `model`; version 2 of `model` was then built from
    `scores`. Follow the *rows* and you go in circles; follow the *versions* and it is a
    straight line, because an edge names a version and a version chain only ever moves forward.
    That is the difference between a foreign key and a lineage edge, in two rows.
    """
    with history_context("feedback: first fit"):
        model = a("feedback: model")
        scores = b("feedback: scores")
        lineage.record_derivation(scores, sources=[model])

    with history_context("feedback: retrained on its own scores"):
        model.name = "feedback: model (retrained)"
        model.save()
        lineage.record_derivation(model, sources=[scores])

    return Scenario("feedback", "a ↔ b, acyclic in versions", model)


def rebuild() -> Scenario:
    """The same target, derived twice, in two runs — so the object page shows two groups.

    The first group stays pinned to the rates as they were when the totals were first computed,
    and reads stale. Keeping it is the whole argument for versioned lineage: "what was this
    built from" and "what would it be built from now" are different questions, and overwriting
    the edge would destroy the only answer to the first one.
    """
    with history_context("rebuild: first pass"):
        rates = a("rebuild: exchange rates")
        totals = a("rebuild: converted totals")
        lineage.record_derivation(totals, sources=[rates])

    with history_context("rebuild: after the rate change"):
        rates.name = "rebuild: exchange rates (updated)"
        rates.save()
        totals.name = "rebuild: converted totals (recomputed)"
        totals.save()
        lineage.record_derivation(totals, sources=[rates])

    return Scenario("rebuild", "one target, two runs, one stale", totals)


@history_context("erased")
def erased_source() -> Scenario:
    """The source is soft-deleted after being consumed. The edge still resolves — a deleted row
    keeps its version rows, which is exactly why deletes are soft in the first place."""
    upload = b("erased: original upload")
    extracted = a("erased: extracted table")
    lineage.record_derivation(extracted, sources=[upload])
    upload.soft_delete()
    return Scenario("erased", "source deleted, edge intact", extracted)


@history_context("hub")
def hub() -> Scenario:
    """One row consumed by many: the "used to build" side, at a size that has to page."""
    shared = b("hub: shared reference list")
    for index in range(1, CONSUMERS + 1):
        consumer = a(f"hub: consumer {index:02d}")
        lineage.record_derivation(consumer, sources=[shared])
    return Scenario("hub", f"{CONSUMERS} rows from 1 source", shared)


def churn() -> Scenario:
    """One row, many versions, every edit its own run.

    A version chain is only worth having if you can tell the states apart, and a chain written
    by one context is a chain that renders as a single revision. Each edit opens its own
    `history_context`, so the object page reads as a list of changes with a name against each —
    which is what "who changed this, and when" actually needs.
    """
    doc = a("churn: working draft")
    for step in range(1, EDITS + 1):
        with history_context(f"churn: edit {step}"):
            doc.name = f"churn: working draft (revision {step})"
            doc.save()
    return Scenario("churn", f"{doc.version} versions of one row", doc)


def restore() -> Scenario:
    """Written, edited, retired, brought back — four states, none of them lost.

    A soft delete is an ordinary UPDATE, so it bumps the version and writes a version row: "was
    deleted" is a state the row had, not the absence of one. Undoing it is another write, with
    the *next* version number rather than a resurrection of the old one, so the chain still
    reads forwards and the earlier states stay exactly where they were.
    """
    with history_context("restore: written"):
        note = a("restore: note")
    with history_context("restore: edited"):
        note.name = "restore: note (edited)"
        note.save()
    with history_context("restore: retired"):
        note.soft_delete()
    with history_context("restore: brought back to how it started"):
        # The state to restore comes out of the history (`history()[0].to_object()` is a ModelA
        # as it was created); it is applied to the live row, because a restore is a write like
        # any other and must go through the same trigger.
        original = note.history()[0].to_object()
        note.name = original.name
        note.deleted_at = None
        note.save()
    return Scenario("restore", f"{note.version} versions incl. a delete", note)


def moving_target() -> Scenario:
    """A source revised twice, with the report rebuilt after each revision.

    The report ends up with three groups of edges pointing at three *different versions of the
    same row* — v1, v2 and v3 of the rate table. Two of them are stale, and both are still the
    truthful answer to "what did the report say last month and why". A pointer to the live row
    could only ever answer the third one.
    """
    with history_context("moving: first report"):
        rates = b("moving: fx table")
        report = a("moving: monthly report")
        lineage.record_derivation(report, sources=[rates])

    for revision in (1, 2):
        with history_context(f"moving: revision {revision}"):
            rates.name = f"moving: fx table (revision {revision})"
            rates.save()
            report.name = f"moving: monthly report (rebuild {revision})"
            report.save()
            lineage.record_derivation(report, sources=[rates])

    versions = {version.version for version in report.sources()}
    return Scenario("moving", f"one source at v{min(versions)}–v{max(versions)}", report)


SHAPES = (
    pipeline,
    merge,
    split,
    diamond,
    feedback,
    rebuild,
    erased_source,
    hub,
    churn,
    restore,
    moving_target,
)


def retire_demo_rows() -> int:
    """Soft-delete the rows of earlier runs, so a fresh graph is not tangled with the last one.

    Soft, not hard: `.delete()` raises in the database (`apps/core/models.py`), and the rows stay
    in the explorer marked deleted — which is the honest picture, since their lineage edges and
    version history are still there.
    """
    retired = 0
    for model in (ModelA, ModelB):
        for row in model.objects.all():
            row.soft_delete()
            retired += 1
    return retired


@click.command()
@click.option("--user", "-u", "username", default="admin", show_default=True, help="Whose tenant.")
@click.option("--clean", is_flag=True, help="Soft-delete the rows of earlier runs first.")
def command(username: str, clean: bool) -> None:
    """Build a lineage graph out of ModelA and ModelB, big enough to have shapes."""
    user = User.objects.filter(username=username).first()
    if user is None:
        raise click.ClickException(f"no user {username!r} — try `manage.py createadmin`")

    # One transaction: a half-built graph is worse than none, and the shapes are only meaningful
    # together. Each shape still opens its own history context inside it.
    with tenant_context(user.pk), transaction.atomic():
        retired = retire_demo_rows() if clean else 0
        scenarios = [build() for build in SHAPES]
        # Counted inside the context: `Lineage` carries the tenant policy like everything else,
        # so the same query outside it is not a smaller number, it is zero.
        edges = lineage.Lineage.objects.count()

    if retired:
        console.print(f"retired {retired} row(s) from earlier runs")

    table = Table(title=f"lineage demo · {user.get_username()}")
    table.add_column("shape", style="bold")
    table.add_column("what it shows")
    # The URL is the useful part of this table, so it folds rather than being truncated.
    table.add_column("open", overflow="fold")
    for scenario in scenarios:
        table.add_row(scenario.name, scenario.shape, _link(user, scenario.root))
    console.print(table)

    log.info(
        "lineage_demo_built",
        user=user.get_username(),
        shapes=len(scenarios),
        retired=retired,
        edges=edges,
    )


def _link(user: User, obj: OwnedModel) -> str:
    """Where to look at this row. The explorer is development-only, so when it is off there is
    nothing useful to print — the id is what a shell would need anyway."""
    meta = type(obj)._meta
    if not settings.EXPLORER_ENABLED:
        return f"{meta.label} {obj.pk}"
    return reverse("explorer:object", args=[user.pk, meta.app_label, meta.model_name, obj.pk])
