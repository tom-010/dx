"""`manage.py lineage_demo` — the generator for the lineage graph the explorer is built to show.

Worth testing because it is the fixture the explorer is demonstrated with: if a shape stops
coming out of it, the pages look fine and say something false. Each assertion here is one of the
shapes named in the command's docstring.
"""

import pytest
from click.testing import CliRunner

from apps.accounts.models import User
from apps.core import lineage
from apps.core.testing import acting_as
from apps.datasets.management.commands import lineage_demo
from apps.datasets.models import ModelA, ModelB

pytestmark = pytest.mark.django_db

runner = CliRunner()


@pytest.fixture
def built(user: User) -> User:
    result = runner.invoke(lineage_demo.command, ["-u", user.get_username()])
    assert result.exit_code == 0, result.output
    return user


def row(model: type[ModelA] | type[ModelB], prefix: str) -> ModelA | ModelB:
    found = model.all_objects.filter(name__startswith=prefix).first()
    assert found is not None, f"no {model.__name__} named {prefix!r}"
    return found


def test_an_unknown_user_is_a_clean_failure(db: None) -> None:
    result = runner.invoke(lineage_demo.command, ["-u", "nobody"])

    assert result.exit_code == 1
    assert "no user 'nobody'" in result.output


def test_merge_gives_one_row_many_parents(built: User) -> None:
    with acting_as(built):
        merged = row(ModelA, "merge: merged report")

        assert len(merged.sources()) == lineage_demo.PARTS


def test_split_gives_one_parent_many_children(built: User) -> None:
    with acting_as(built):
        whole = row(ModelA, "split: whole document")

        assert len(whole.derived()) == lineage_demo.PIECES


def test_the_diamond_has_two_parents_that_share_a_grandparent(built: User) -> None:
    """The shape a `parent_id` column cannot express."""
    with acting_as(built):
        joined = row(ModelA, "diamond: joined result")
        # Naming the model filters *and* types the result — `.to_object()` is a ModelB here.
        parents = joined.sources(ModelB)

        assert {version.to_object().name for version in parents} == {
            "diamond: left projection",
            "diamond: right projection",
        }
        grandparents = {
            source.object_id for version in parents for source in version.to_object().sources()
        }
        assert len(grandparents) == 1


def test_feedback_is_a_cycle_between_rows_but_not_between_versions(built: User) -> None:
    """`scores` came from v1 of the model; v2 of the model came from `scores`. Following rows
    goes in circles; following versions does not, because a version chain only moves forward."""
    with acting_as(built):
        model = row(ModelA, "feedback: model")
        scores = row(ModelB, "feedback: scores")

        assert [v.object_id for v in model.sources()] == [scores.pk]
        assert [v.object_id for v in model.derived()] == [scores.pk]
        # The edge into `scores` names version 1; the model is at version 2 by now.
        assert scores.sources()[0].version == 1
        assert model.version == 2


def test_rebuild_keeps_the_edge_it_replaced(built: User) -> None:
    """Two runs against one target: the older group stays pinned to what was actually consumed,
    which is the only record of why the first result looked the way it did."""
    with acting_as(built):
        totals = row(ModelA, "rebuild: converted totals")
        versions = [version.version for version in totals.sources()]

        assert versions == [1, 2]  # the rates as first read, and as they are now
        assert not totals.sources()[0].is_current()
        assert totals.sources()[1].is_current()


def test_staleness_does_not_propagate_down_the_pipeline(built: User) -> None:
    """The head of the chain changed, so exactly one derivation is stale — the one built
    directly from it. Everything further down still matches the version *it* consumed, and only
    goes stale once its own source is rebuilt. A work list is one level deep at a time."""
    with acting_as(built):
        raw = row(ModelB, "pipeline: raw upload")
        parsed = row(ModelA, "pipeline: parsed rows")

        assert lineage.stale_derivations(raw).count() == 1
        assert lineage.stale_derivations(parsed).count() == 0


def test_an_erased_source_keeps_its_edge(built: User) -> None:
    """Soft delete, so the version rows survive and the edge still resolves — the reason nothing
    in this schema is ever hard-deleted.

    The version the edge names is *not* itself deleted: the delete was a later write, so it made
    a new version. The edge keeps pointing at the state that was actually consumed, while the
    live row is gone from `objects` entirely.
    """
    with acting_as(built):
        extracted = row(ModelA, "erased: extracted table")
        upload = extracted.sources(ModelB)[0]

        assert upload.to_object().name == "erased: original upload"
        assert not upload.deleted  # as it stood when it was read
        assert not ModelB.objects.filter(pk=upload.object_id).exists()  # ...but gone now
        assert ModelB.all_objects.get(pk=upload.object_id).deleted_at is not None


def test_the_hub_is_used_by_many(built: User) -> None:
    with acting_as(built):
        shared = row(ModelB, "hub: shared reference list")

        assert len(shared.derived()) == lineage_demo.CONSUMERS


def test_clean_retires_the_previous_run(built: User) -> None:
    """Soft-deleted, not gone: a second run must not tangle with the first, but the rows keep
    their history and their edges."""
    with acting_as(built):
        before = ModelA.objects.count()

    result = runner.invoke(lineage_demo.command, ["-u", built.get_username(), "--clean"])
    assert result.exit_code == 0, result.output

    with acting_as(built):
        assert ModelA.objects.count() == before  # the same graph again, freshly built
        assert ModelA.all_objects.count() == before * 2  # ...and the retired one still there
        assert ModelA.all_objects.filter(deleted_at__isnull=False).count() == before


# --- the shapes built out of version churn --------------------------------------------------------


def test_churn_leaves_a_long_chain_of_distinct_saves(built: User) -> None:
    """Each edit its own run, so the chain reads as a list of changes with a name against each
    rather than as one undifferentiated revision."""
    with acting_as(built):
        doc = row(ModelA, "churn: working draft")
        history = doc.history()

        assert doc.version == lineage_demo.EDITS + 1
        assert len(history) == lineage_demo.EDITS + 1
        assert history[0].to_object().name == "churn: working draft"
        assert history[-1].is_current()
        # Every save opened its own context, so no two versions share one.
        contexts = {version.event.pgh_context_id for version in history}
        assert len(contexts) == len(history)


def test_a_delete_and_an_undelete_are_both_versions(built: User) -> None:
    """ "Was deleted" is a state the row had, and undoing it gets the *next* version number —
    the chain only ever moves forward, so nothing is lost either way."""
    with acting_as(built):
        note = row(ModelA, "restore: note")
        states = [(version.version, version.deleted) for version in note.history()]

        assert states == [(1, False), (2, False), (3, True), (4, False)]
        assert note.deleted_at is None
        assert note.name == note.history()[0].to_object().name  # back to how it started


def test_one_source_consumed_at_three_different_versions(built: User) -> None:
    """The report was rebuilt after each revision of the rates, so its edges name v1, v2 and v3
    of the same row. Two are stale, and both are still the truthful answer to what the earlier
    reports were built from."""
    with acting_as(built):
        report = row(ModelA, "moving: monthly report")
        sources = report.sources(ModelB)

        assert [version.version for version in sources] == [1, 2, 3]
        assert {version.object_id for version in sources} == {sources[0].object_id}
        assert [version.is_current() for version in sources] == [False, False, True]
        assert report.version == 3


def test_a_fan_in_can_be_partly_stale(built: User) -> None:
    """Two of five parts were corrected afterwards: freshness is per edge, not per row."""
    with acting_as(built):
        merged = row(ModelA, "merge: merged report")
        current = [version.is_current() for version in merged.sources()]

        assert len(current) == lineage_demo.PARTS
        assert current.count(False) == 2
