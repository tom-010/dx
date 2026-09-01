"""Merging — and the lineage graph it builds.

This is what the notes app exists to show: a merge records *which version* of each source it
read, so the graph keeps telling the truth after those sources are edited.
"""

import pytest
from django.test import Client

from apps.accounts.models import User
from apps.core import lineage
from apps.core.testing import acting_as
from apps.notes.api import create_note_for, get_note_for, merge_notes_for
from apps.notes.models import Note, NoteId

pytestmark = pytest.mark.django_db


def source_titles(note: Note) -> list[str]:
    """The titles of `note`'s sources *as they read when it was merged from them*."""
    return sorted(source.to_object().title for source in note.sources(Note))


def merge(user: User, *notes: Note, title: str) -> Note:
    return merge_notes_for(user, [NoteId(note.pk) for note in notes], title=title)


# --- Merging ------------------------------------------------------------------------------------


def test_merge_records_one_edge_per_source(user: User) -> None:
    with acting_as(user):
        first = create_note_for(user, title="Monday", body="ran 5k")
        second = create_note_for(user, title="Tuesday", body="rest day")
        second.body = "rest day (edited)"
        second.save()

        merged = merge(user, first, second, title="This week")

        assert "ran 5k" in merged.body and "rest day (edited)" in merged.body
        edges = {edge.source_obj_id: edge.source_version for edge in lineage.sources_of(merged)}
        # Each source is pinned to its *own* current version, not to a shared number.
        assert edges == {first.pk: 1, second.pk: 2}


def test_merging_leaves_the_sources_alone(user: User) -> None:
    """A merge adds a note; it does not consume the ones it read."""
    with acting_as(user):
        first = create_note_for(user, title="A")
        second = create_note_for(user, title="B")

        merge(user, first, second, title="A+B")

        assert Note.objects.filter(pk__in=[first.pk, second.pk]).count() == 2
        assert Note.objects.get(pk=first.pk).version == 1


def test_the_merge_still_names_the_version_it_read_after_an_edit(user: User) -> None:
    with acting_as(user):
        first = create_note_for(user, title="Recipe", body="one egg")
        second = create_note_for(user, title="Notes", body="serves two")
        merged = merge(user, first, second, title="Dinner")

        first.title = "Recipe v2"
        first.body = "two eggs"
        first.save()

        assert source_titles(merged) == ["Notes", "Recipe"]  # not "Recipe v2"
        was = next(v for v in merged.sources(Note) if v.object_id == first.pk)
        assert was.to_object().body == "one egg"
        assert was.is_current() is False  # the source has moved on since the merge
        assert lineage.stale_derivations(first).count() == 1


def test_merge_unions_the_tags_of_its_sources(user: User) -> None:
    with acting_as(user):
        first = create_note_for(user, title="A", tags="walk, birds")
        second = create_note_for(user, title="B", tags="Birds, weather")

        merged = merge(user, first, second, title="A+B")

    assert merged.tags == "birds, walk, weather"  # deduplicated case-insensitively


def test_merge_needs_two_different_notes(auth_client: Client, user: User) -> None:
    with acting_as(user):
        note = create_note_for(user, title="Only one")

    response = auth_client.post(
        "/api/notes/merge",
        {"note_ids": [str(note.pk), str(note.pk)], "title": "Nope"},
        content_type="application/json",
    )

    assert response.status_code == 400
    assert "at least two" in response.json()["detail"]


def test_merging_another_tenants_note_is_a_404(
    auth_client: Client, user: User, other_user: User
) -> None:
    with acting_as(user):
        mine = create_note_for(user, title="mine")
    with acting_as(other_user):
        theirs = create_note_for(other_user, title="theirs")

    response = auth_client.post(
        "/api/notes/merge",
        {"note_ids": [str(mine.pk), str(theirs.pk)], "title": "Stolen"},
        content_type="application/json",
    )

    assert response.status_code == 404
    with acting_as(user):
        assert lineage.Lineage.objects.count() == 0


# --- The graph ----------------------------------------------------------------------------------


def build_showcase(user: User) -> dict[str, Note]:
    """Three notes, two merges that share a source, and a merge of those two.

        Monday  Tuesday  Wednesday
             \\  /     \\  /
             Early    Late
                \\    /
                 Week

    Merging alone is enough for a real graph: Tuesday has two children (branching) and Week has
    two parents that share a grandparent (a diamond).
    """
    monday = create_note_for(user, title="Monday", body="ran 5k")
    tuesday = create_note_for(user, title="Tuesday", body="rest day")
    wednesday = create_note_for(user, title="Wednesday", body="swim")
    early = merge(user, monday, tuesday, title="Early week")
    late = merge(user, tuesday, wednesday, title="Late week")
    week = merge(user, early, late, title="Week")
    return {
        "monday": monday,
        "tuesday": tuesday,
        "wednesday": wednesday,
        "early": early,
        "late": late,
        "week": week,
    }


def test_the_graph_walks_both_directions(user: User) -> None:
    with acting_as(user):
        notes = build_showcase(user)

        graph = lineage.graph(notes["early"], depth=3)

        by_label = {node.label: node for node in graph.nodes}
        assert set(by_label) == {
            "Monday",
            "Tuesday",
            "Wednesday",
            "Early week",
            "Late week",
            "Week",
        }
        # A source is one generation below, something merged from it one above. The two nodes
        # only reachable by turning around mid-walk land where they belong: the sibling merge
        # (down to Week, back up) is level with the root, and Wednesday — its other source —
        # sits with the rest of the raw notes.
        assert by_label["Early week"].depth == 0
        assert by_label["Late week"].depth == 0
        assert by_label["Monday"].depth == -1
        assert by_label["Tuesday"].depth == -1
        assert by_label["Wednesday"].depth == -1
        assert by_label["Week"].depth == 1
        assert len(graph.edges) == 6


def test_the_graph_marks_edges_whose_source_moved_on(user: User) -> None:
    with acting_as(user):
        notes = build_showcase(user)
        notes["tuesday"].body = "rest day, revised"
        notes["tuesday"].save()

        graph = lineage.graph(notes["week"], depth=3)
        stale = {edge.source_id for edge in graph.edges if edge.is_stale}

        # Tuesday fed both weekly summaries, so editing it makes both edges stale at once.
        assert stale == {notes["tuesday"].pk}
        assert len([edge for edge in graph.edges if edge.is_stale]) == 2


def test_the_graph_depth_is_bounded(user: User) -> None:
    """A page asks for a neighbourhood, not for the tenant's whole history."""
    with acting_as(user):
        note = create_note_for(user, title="0")
        for step in range(1, 5):
            other = create_note_for(user, title=f"side {step}")
            note = merge(user, note, other, title=str(step))

        near = lineage.graph(note, depth=1)
        far = lineage.graph(note, depth=4)

    assert sorted(node.label for node in near.nodes) == ["3", "4", "side 4"]
    assert len(far.nodes) == 9
    assert min(node.depth for node in far.nodes) == -4


def test_the_graph_endpoint_is_tenant_isolated(
    auth_client: Client, user: User, other_user: User
) -> None:
    with acting_as(other_user):
        theirs = create_note_for(other_user, title="theirs")
    with acting_as(user):
        notes = build_showcase(user)

    body = auth_client.get(f"/api/lineage/note/{notes['early'].pk}").json()

    assert body["root_id"] == str(notes["early"].pk)
    assert len(body["nodes"]) == 6
    assert len(body["edges"]) == 6
    assert auth_client.get(f"/api/lineage/note/{theirs.pk}").status_code == 404


def test_the_graph_endpoint_rejects_an_unknown_resource(auth_client: Client, user: User) -> None:
    with acting_as(user):
        note = create_note_for(user, title="x")
    response = auth_client.get(f"/api/lineage/nonsense/{note.pk}")
    assert response.status_code == 404
    assert "nonsense" in response.json()["detail"]


def test_a_note_with_no_merges_has_an_empty_graph(auth_client: Client, user: User) -> None:
    with acting_as(user):
        note = create_note_for(user, title="alone")

    body = auth_client.get(f"/api/lineage/note/{note.pk}").json()

    assert [node["label"] for node in body["nodes"]] == ["alone"]
    assert body["edges"] == []


def test_a_deleted_note_still_appears_in_the_graph(user: User) -> None:
    """Deletes are soft precisely so that this keeps working: the merged note must not lose the
    record of where it came from because a source was tidied away."""
    with acting_as(user):
        first = create_note_for(user, title="Draft")
        second = create_note_for(user, title="Keep")
        merged = merge(user, first, second, title="Both")
        get_note_for(user, NoteId(first.pk)).soft_delete()

        graph = lineage.graph(merged, depth=2)

        deleted = [node for node in graph.nodes if node.deleted]
        assert [node.label for node in deleted] == ["Draft"]
        assert source_titles(merged) == ["Draft", "Keep"]


def test_the_history_page_names_the_source_note(auth_client: Client, user: User) -> None:
    """The "Derived from" link has to say which note, in words. Event rows are generated classes
    that cannot borrow `VersionedModel.__str__`, so the label goes through the same field preference
    (`apps/core/revisions.py::row_label`) — `title` here, `name` on other models."""
    with acting_as(user):
        first = create_note_for(user, title="Monday")
        second = create_note_for(user, title="Tuesday")
        merged = merge(user, first, second, title="This week")

    body = auth_client.get(f"/api/history/note/{merged.pk}").json()

    sources = body["groups"][0]["revisions"][0]["sources"]
    assert sorted(source["label"] for source in sources) == ["Monday", "Tuesday"]
