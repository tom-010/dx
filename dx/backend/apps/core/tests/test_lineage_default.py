"""Lineage as the default: the write itself says what it was built from and which step did it.

`record_derivation()` is correct and complete and it is a second statement, which is exactly why
it gets forgotten. These tests pin the shape that inverts that: `Model.create(...)` and
`save(...)` take `sources=` and `operation=`, a `deriving()` block supplies sources to every write
inside it, and not recording lineage is something you have to say (`sources=[]`).
"""

import pytest

from apps.accounts.models import User
from apps.core import lineage, revisions
from apps.core.history import history_context
from apps.core.testing import acting_as
from apps.datasets.models import Dataset

pytestmark = pytest.mark.django_db


def label_of(obj: Dataset) -> str:
    """The `history_context` label the row's current version was written under."""
    context_id = obj.history()[-1].event.pgh_context_id
    assert context_id is not None
    return revisions.context_sources({context_id})[context_id]


# --- creating -------------------------------------------------------------------------------------


def test_create_writes_the_row_its_edges_and_its_step_in_one_statement(user: User) -> None:
    with acting_as(user):
        source = Dataset.create(operation=None, sources=[], name="source")
        derived = Dataset.create(operation="summarise", sources=[source], name="derived")

        assert [v.object_id for v in derived.sources()] == [source.pk]
        assert label_of(derived) == "summarise"
        # The edge carries the same step as the version it feeds.
        (edge,) = lineage.all_sources_of(derived)
        assert edge.pgh_context == derived.history()[-1].event.pgh_context_id


def test_constructing_then_saving_takes_the_same_keywords(user: User) -> None:
    with acting_as(user):
        source = Dataset.create(operation=None, sources=[], name="source")
        derived = Dataset(name="derived")
        derived.save(sources=[source], operation="via save")

        assert [v.object_id for v in derived.sources()] == [source.pk]
        assert label_of(derived) == "via save"


def test_none_defers_to_the_enclosing_blocks(user: User) -> None:
    """`None` is the third answer: not "from nothing" (`[]`), but "whatever the block says"."""
    with acting_as(user):
        source = Dataset.create(operation=None, sources=[], name="source")
        with history_context("in a block"), lineage.deriving(source):
            derived = Dataset.create(operation=None, sources=None, name="derived")

        assert [v.object_id for v in derived.sources()] == [source.pk]
        assert label_of(derived) == "in a block"


# --- updating -------------------------------------------------------------------------------------


def test_a_rebuild_records_edges_against_the_new_version(user: User) -> None:
    with acting_as(user):
        rates = Dataset.create(operation=None, sources=[], name="rates")
        report = Dataset.create(operation="first", sources=[rates], name="report")
        rates.name = "rates (revised)"
        rates.save(operation=None, sources=[])

        report.name = "report (rebuilt)"
        report.save(sources=[rates], operation="rebuild")

        versions = [v.version for v in report.sources()]
        assert versions == [1, 2]  # one edge per build, each pinned to the rates as then read
        assert report.history()[-1].sources()[0].version == 2  # and only the new one on v2
        assert label_of(report) == "rebuild"


def test_a_partial_save_takes_the_same_keywords(user: User) -> None:
    with acting_as(user):
        source = Dataset.create(operation=None, sources=[], name="source")
        derived = Dataset.create(operation=None, sources=[], name="derived")
        derived.name = "derived (renamed)"
        derived.save(update_fields=["name"], sources=[source], operation="partial")

        assert [v.object_id for v in derived.sources()] == [source.pk]
        assert label_of(derived) == "partial"


def test_a_bulk_update_is_versioned_and_labelled_but_carries_no_sources(user: User) -> None:
    """No `save()` runs, so there is nowhere for `sources=` to go: the one write path lineage
    cannot ride on. The version and its label still happen — those are the trigger's and the
    context's, not `save()`'s."""
    with acting_as(user):
        source = Dataset.create(operation=None, sources=[], name="source")
        row = Dataset.create(operation=None, sources=[], name="row")
        with history_context("bulk"), lineage.deriving(source):
            Dataset.objects.filter(pk=row.pk).update(name="row (bulk)")
        row.refresh_from_db()

        assert row.version == 2
        assert label_of(row) == "bulk"
        assert row.sources() == []


# --- opting out, and the edges you do not want ---------------------------------------------------


def test_an_empty_sources_list_opts_one_write_out_of_the_block(user: User) -> None:
    with acting_as(user):
        source = Dataset.create(operation=None, sources=[], name="source")
        with lineage.deriving(source):
            derived = Dataset.create(operation=None, sources=None, name="derived")
            unrelated = Dataset.create(operation=None, sources=[], name="unrelated")

        assert [v.object_id for v in derived.sources()] == [source.pk]
        assert unrelated.sources() == []


def test_an_explicit_sources_list_overrides_the_block(user: User) -> None:
    with acting_as(user):
        block_source = Dataset.create(operation=None, sources=[], name="block source")
        own_source = Dataset.create(operation=None, sources=[], name="own source")
        with lineage.deriving(block_source):
            derived = Dataset.create(operation=None, sources=[own_source], name="derived")

        assert [v.object_id for v in derived.sources()] == [own_source.pk]


def test_a_nested_block_replaces_the_outer_one(user: User) -> None:
    """A nested step has its own inputs; merging them would claim more than it knows."""
    with acting_as(user):
        outer = Dataset.create(operation=None, sources=[], name="outer")
        inner = Dataset.create(operation=None, sources=[], name="inner")
        with lineage.deriving(outer):
            with lineage.deriving(inner):
                from_inner = Dataset.create(operation=None, sources=None, name="from inner")
            from_outer = Dataset.create(operation=None, sources=None, name="from outer")

        assert [v.object_id for v in from_inner.sources()] == [inner.pk]
        assert [v.object_id for v in from_outer.sources()] == [outer.pk]


def test_deleting_inside_a_block_records_nothing(user: User) -> None:
    """Retiring a row is not deriving it from anything."""
    with acting_as(user):
        source = Dataset.create(operation=None, sources=[], name="source")
        row = Dataset.create(operation=None, sources=[], name="row")
        with lineage.deriving(source):
            row.delete()

        assert row.sources() == []
        assert row.history()[-1].deleted


def test_repeats_and_the_row_itself_are_not_sources(user: User) -> None:
    """`uniq_lineage_edge` would refuse the second copy anyway; the row is not built from itself."""
    with acting_as(user):
        source = Dataset.create(operation=None, sources=[], name="source")
        derived = Dataset.create(operation=None, sources=[], name="derived")
        derived.name = "derived (rebuilt)"
        derived.save(operation=None, sources=[source, source, derived])

        assert [v.object_id for v in derived.sources()] == [source.pk]


def test_the_edge_names_the_line_that_asked_for_it(user: User) -> None:
    """Not the `save()` plumbing, and not `create()`'s either: both are in `apps/core/`, which
    is skipped at capture, so the innermost frame left is the line that asked for the row."""
    with acting_as(user):
        source = Dataset.create(operation=None, sources=[], name="source")
        with lineage.deriving(source):
            derived = Dataset.create(operation=None, sources=None, name="derived")
        (edge,) = lineage.all_sources_of(derived)

        caller = edge.caller
        assert caller is not None
        assert caller.file.endswith("test_lineage_default.py")
        assert caller.func == "test_the_edge_names_the_line_that_asked_for_it"
        assert "Dataset.create" in caller.code


# --- the history context, nested ------------------------------------------------------------------


def test_a_nested_history_context_is_its_own_context(user: User) -> None:
    """pghistory would merge the inner label into the outer context, relabelling every write of
    the request. A step is a run of its own."""
    with acting_as(user), history_context("request"):
        before = Dataset.create(operation=None, sources=[], name="before")
        with history_context("step"):
            inside = Dataset.create(operation=None, sources=[], name="inside")
        after = Dataset.create(operation=None, sources=[], name="after")

        assert label_of(before) == "request"
        assert label_of(inside) == "step"
        assert label_of(after) == "request"
        outer_ids = {r.history()[-1].event.pgh_context_id for r in (before, after)}
        assert len(outer_ids) == 1  # the outer context survived the nesting intact
        assert inside.history()[-1].event.pgh_context_id not in outer_ids


def test_a_per_write_operation_inside_a_request_does_not_relabel_the_request(user: User) -> None:
    with acting_as(user), history_context("api"):
        first = Dataset.create(operation=None, sources=[], name="first")
        labelled = Dataset.create(operation="import", sources=[], name="labelled")
        last = Dataset.create(operation=None, sources=[], name="last")

        assert (label_of(first), label_of(labelled), label_of(last)) == ("api", "import", "api")


def test_an_operation_does_not_leak_to_the_writes_after_it(user: User) -> None:
    """pghistory injects its context with `set_config(..., true)` — transaction-local — and only
    re-injects while a context is open. So a labelled write used to leave its label set for the
    rest of the transaction, and every later write in the same request was recorded under a step
    that had already finished (`history._forget_injected_context`)."""
    with acting_as(user):  # one transaction around both writes, as a request would have
        labelled = Dataset.create(operation="summarise notes", sources=[], name="labelled")
        after = Dataset.create(operation=None, sources=[], name="after")

        assert label_of(labelled) == "summarise notes"
        assert after.history()[-1].event.pgh_context_id is None


def test_the_same_holds_for_a_block(user: User) -> None:
    with acting_as(user):
        with history_context("a step"):
            inside = Dataset.create(operation=None, sources=[], name="inside")
        after = Dataset.create(operation=None, sources=[], name="after the block")

        assert label_of(inside) == "a step"
        assert after.history()[-1].event.pgh_context_id is None
