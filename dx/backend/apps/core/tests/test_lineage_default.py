"""Lineage as the default: the write itself says which operation made it and what it was built
from.

`record_derivation()` is correct and complete and it is a second statement, which is exactly why
it gets forgotten. These tests pin the shape that inverts that: `Model.create(...)` and
`save(...)` *require* `operation=` and `sources=`, a `deriving()` block supplies sources to every
write inside it, and not recording lineage is something you have to say (`sources=[]`).
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from pytest_django.fixtures import Settings

from apps.accounts.models import User
from apps.core import lineage, revisions, source
from apps.core.history import history_context
from apps.core.source import SourceSnippet
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
        derived.save(operation="via save", sources=[source])

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


def test_objects_create_is_refused_with_directions(user: User) -> None:
    """A manager's `create()` cannot carry the two keywords, so it cannot be explicit — and a
    write that cannot state its lineage is not a write this project makes."""
    with acting_as(user), pytest.raises(TypeError, match="Dataset.create"):
        Dataset.objects.create(name="no lineage")


def test_the_keywords_have_no_defaults(user: User) -> None:
    with acting_as(user):
        row = Dataset.create(operation=None, sources=[], name="row")
        with pytest.raises(TypeError):
            row.save()  # type: ignore[call-arg]  # the omission under test
        with pytest.raises(TypeError):
            Dataset.create(name="no keywords")  # type: ignore[call-arg]


def test_a_description_needs_an_operation_to_describe(user: User) -> None:
    with acting_as(user), pytest.raises(ValueError, match="operation="):
        Dataset.create(operation=None, sources=[], operation_description="of nothing", name="row")


# --- updating -------------------------------------------------------------------------------------


def test_a_rebuild_records_edges_against_the_new_version(user: User) -> None:
    with acting_as(user):
        rates = Dataset.create(operation=None, sources=[], name="rates")
        report = Dataset.create(operation="first", sources=[rates], name="report")
        rates.name = "rates (revised)"
        rates.save(operation=None, sources=[])

        report.name = "report (rebuilt)"
        report.save(operation="rebuild", sources=[rates])

        versions = [v.version for v in report.sources()]
        assert versions == [1, 2]  # one edge per build, each pinned to the rates as then read
        assert report.history()[-1].sources()[0].version == 2  # and only the new one on v2
        assert label_of(report) == "rebuild"


def test_a_partial_save_takes_the_same_keywords(user: User) -> None:
    with acting_as(user):
        source = Dataset.create(operation=None, sources=[], name="source")
        derived = Dataset.create(operation=None, sources=[], name="derived")
        derived.name = "derived (renamed)"
        derived.save(operation="partial", sources=[source], update_fields=["name"])

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


# --- who wrote it: the stack, on edges and on versions --------------------------------------------


def test_a_version_records_the_code_that_wrote_it(user: User) -> None:
    """The same record `Lineage.stack` keeps for an edge, for every version.

    Python never inserts a version row — the capture trigger does — so the stack is handed over
    in a transaction-local setting the column default reads
    (`lineage.declare_write_origin`, `history.Event`).
    """
    with acting_as(user):
        dataset = Dataset.create(operation=None, sources=[], name="written here")
        version = dataset.history()[-1]  # event rows are tenant data too: read them in context
        caller = version.caller

        assert caller is not None
        assert caller.module == __name__
        assert caller.func == "test_a_version_records_the_code_that_wrote_it"
        assert "Dataset.create" in caller.code
        assert version.release  # the build, exactly as an edge records it


def test_an_edit_records_the_line_that_edited_it(user: User) -> None:
    with acting_as(user):
        dataset = Dataset.create(operation=None, sources=[], name="first")
        dataset.name = "second"
        dataset.save(operation=None, sources=[])

        first, second = dataset.history()
        assert first.caller is not None and "Dataset.create" in first.caller.code
        assert second.caller is not None and "dataset.save" in second.caller.code
        assert first.caller.line != second.caller.line


def test_a_write_that_bypasses_save_records_no_stack(user: User) -> None:
    """A bulk `.update()` never calls `save()`, so nothing declares an origin. The version is
    still written — the trigger sees to that — and honestly says it does not know who wrote it,
    rather than inheriting the previous write's stack."""
    with acting_as(user):
        dataset = Dataset.create(operation=None, sources=[], name="before")
        Dataset.objects.filter(pk=dataset.pk).update(name="after")
        dataset.refresh_from_db()

        first, second = dataset.history()
        assert first.stack and first.caller is not None
        assert second.version == 2
        assert second.stack == []
        assert second.caller is None
        assert second.release == ""


def test_only_this_projects_frames_are_recorded_and_all_of_them(user: User) -> None:
    """The filter runs at capture and asks the interpreter what is *not* ours (`lineage.is_ours`):
    forty frames of WSGI, middleware and ninja are not what "who wrote this" asks. But *every*
    frame of our own code stays — a write five calls deep into the project's code records all
    five, outermost first, so the chain that led there can be read back, not just its last step.
    """

    def step_one() -> Dataset:
        return step_two()

    def step_two() -> Dataset:
        return step_three()

    def step_three() -> Dataset:
        return step_four()

    def step_four() -> Dataset:
        return step_five()

    def step_five() -> Dataset:
        return Dataset.create(operation="deep", sources=[], name="five deep")

    with acting_as(user):
        dataset = step_one()
        stack = dataset.history()[-1].stack
    funcs = [frame.func for frame in stack]

    assert all(frame.ours for frame in stack)  # nothing from Django, pytest or the stdlib
    assert not any("site-packages" in frame.file for frame in stack)
    # The whole chain, in calling order — not merely the innermost frame.
    assert funcs[-5:] == ["step_one", "step_two", "step_three", "step_four", "step_five"]
    assert funcs[-6] == "test_only_this_projects_frames_are_recorded_and_all_of_them"
    # And none of the recording mechanism itself, which would otherwise be every stack's tail.
    assert not any(frame.module in ("apps.core.lineage", "apps.core.models") for frame in stack)


def test_ours_is_decided_by_the_interpreter_not_by_a_list() -> None:
    """No package list to keep extending: whatever the interpreter does not claim is ours.

    Checked against the roots the running interpreter reports, so the same assertions hold in
    the container, where the venv and the stdlib live somewhere else entirely.
    """
    import sys

    assert lineage.is_ours(__file__)
    assert lineage.is_ours(str(Path(__file__).parents[3] / "manage.py"))
    assert lineage.is_ours("/anywhere/else/a_new_app/api.py")  # a package added tomorrow
    assert lineage.is_ours("<stdin>")  # typed into a shell — worth knowing about

    assert not lineage.is_ours(f"{sys.prefix}/lib/python3/site-packages/django/db/models/base.py")
    assert not lineage.is_ours(f"{sys.base_prefix}/lib/python3/contextlib.py")
    assert not lineage.is_ours("<frozen importlib._bootstrap>")


# --- the source behind every frame ---------------------------------------------------------------


def test_a_frame_references_the_source_of_the_function_it_was_in(user: User) -> None:
    """Not a window around the line: the whole function, keyed by its content, stored once
    (`apps/core/source.py`). The frame keeps `first_line`, so the executing line can be found
    inside the text without needing the file."""
    with acting_as(user):
        dataset = Dataset.create(operation=None, sources=[], name="with source")
        frame = dataset.history()[-1].caller

    assert frame is not None and frame.sha
    snippet = SourceSnippet.objects.get(sha=frame.sha)
    assert snippet.text.startswith("def test_a_frame_references_the_source_of_the_function")
    assert source.digest(snippet.text) == frame.sha
    # `first_line` + the text reproduce the executing line the frame recorded.
    lines = snippet.text.splitlines()
    assert lines[frame.line - frame.first_line].strip() == frame.code


def test_the_same_function_is_stored_once_however_often_it_writes(
    user: User, django_capture_on_commit_callbacks: Callable[..., AbstractContextManager[Any]]
) -> None:
    """Every `save()` records a stack, the same call site fires thousands of times, and the source
    changes in one run out of many. After the first commit the process knows the sha and Postgres
    is not asked again — no INSERT, no SELECT — which is what makes recording the whole function
    affordable on the hot path."""

    def write(name: str) -> None:
        Dataset.create(operation=None, sources=[], name=name)

    with acting_as(user), django_capture_on_commit_callbacks(execute=True):
        write("first")  # stores the snippets and, on "commit", remembers them
    with acting_as(user), CaptureQueriesContext(connection) as queries:
        write("second")

    # Two distinct functions were on the stack — the nested `write` and this test — so two rows,
    # each stored once. The nested one keeps its indentation: the text is the exact slice of the
    # file, so `contains` finds both and the leading `def` singles out the inner one.
    snippets = [s.text for s in SourceSnippet.objects.filter(text__contains="def write(name: str)")]
    assert len(snippets) == 2
    assert sum(text.lstrip().startswith("def write(") for text in snippets) == 1
    assert not any("core_sourcesnippet" in q["sql"] for q in queries.captured_queries)


def test_a_rolled_back_write_does_not_make_the_process_trust_its_snippets(user: User) -> None:
    """The caches are marked on commit only. A test's transaction never commits, so nothing is
    remembered here — and the second write must therefore still insert (idempotently)."""
    with acting_as(user):
        Dataset.create(operation=None, sources=[], name="first")
    with acting_as(user), CaptureQueriesContext(connection) as queries:
        Dataset.create(operation=None, sources=[], name="second")

    assert any(
        "core_sourcesnippet" in q["sql"] and "ON CONFLICT" in q["sql"]
        for q in queries.captured_queries
    )


def test_an_edge_and_its_version_share_the_snippet(user: User) -> None:
    """One write, one function, one row — referenced from the version and from every edge."""
    with acting_as(user):
        origin = Dataset.create(operation=None, sources=[], name="origin")
        derived = Dataset.create(operation="derive", sources=[origin], name="derived")
        version_frame = derived.history()[-1].caller
        (edge,) = lineage.all_sources_of(derived)
        edge_frame = edge.caller

    assert version_frame is not None and edge_frame is not None
    assert version_frame.sha == edge_frame.sha
    assert SourceSnippet.objects.filter(sha=edge_frame.sha).count() == 1


def test_recording_source_needs_no_git(
    user: User, monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """The text comes from the interpreter, not from a checkout: with no git binary at all the
    snippet is still stored, and only the build falls back to "dev"."""
    import subprocess

    def no_git(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("git")

    settings.APP_VERSION = "dev"
    lineage._git_release.cache_clear()
    monkeypatch.setattr(subprocess, "run", no_git)
    try:
        with acting_as(user):
            dataset = Dataset.create(operation=None, sources=[], name="no git here")
            version = dataset.history()[-1]
    finally:
        lineage._git_release.cache_clear()

    assert version.release == "dev"
    assert version.caller is not None and version.caller.sha
    assert SourceSnippet.objects.filter(sha=version.caller.sha).exists()


def test_a_frame_without_a_file_has_no_source(user: User) -> None:
    """`<stdin>` and frozen modules have nothing `linecache` can read: no sha, no row, no error."""
    first, last, text = source.function_source(compile("x = 1", "<stdin>", "exec"))

    assert (first, last, text) == (1, 1, "")
