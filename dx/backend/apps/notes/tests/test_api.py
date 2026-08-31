import uuid
from collections.abc import Callable

import pytest
from django.test import Client

from apps.accounts.models import User
from apps.core import history, revisions
from apps.core.testing import acting_as
from apps.notes.api import create_note_for, get_note_for, patch_note_for
from apps.notes.models import Note, NoteId
from apps.notes.schemas import NotePatch

pytestmark = pytest.mark.django_db


def test_create_and_list(auth_client: Client) -> None:
    created = auth_client.post(
        "/api/notes",
        {"title": "First", "body": "d", "tags": "walk, birds"},
        content_type="application/json",
    )

    assert created.status_code == 201
    body = created.json()
    assert (body["title"], body["body"], body["version"]) == ("First", "d", 1)
    assert body["tags"] == "birds, walk"  # normalised on write

    listed = auth_client.get("/api/notes")
    assert listed.status_code == 200
    assert listed.json() == {"items": [body], "count": 1}


def test_get_put_patch_delete(auth_client: Client, user: User) -> None:
    with acting_as(user):  # services need a tenant context; requests get it from the middleware
        note = create_note_for(user, title="Temp", body="d")
    url = f"/api/notes/{note.pk}"

    assert auth_client.get(url).status_code == 200

    replaced = auth_client.put(url, {"title": "Full"}, content_type="application/json")
    assert replaced.status_code == 200
    assert (replaced.json()["title"], replaced.json()["body"]) == ("Full", "")
    assert replaced.json()["version"] == 2  # the save is a version, reported back

    patched = auth_client.patch(url, {"body": "x"}, content_type="application/json")
    assert (patched.json()["title"], patched.json()["body"]) == ("Full", "x")

    assert auth_client.delete(url).status_code == 204
    assert auth_client.get(url).json() == {"detail": "Note not found"}


def test_other_users_get_404(
    user: User, other_user: User, client_for: Callable[[User], Client]
) -> None:
    with acting_as(user):
        note = create_note_for(user, title="mine")
    other = client_for(other_user)

    assert other.get("/api/notes").json() == {"items": [], "count": 0}
    assert other.get(f"/api/notes/{note.pk}").status_code == 404
    assert other.delete(f"/api/notes/{note.pk}").status_code == 404


def test_service_raises_for_unknown_id(user: User) -> None:
    with acting_as(user), pytest.raises(HttpError):
        get_note_for(user, NoteId(uuid.uuid7()))


# --- Editing and tags ---------------------------------------------------------------------------


def test_editing_a_note_is_a_new_version(auth_client: Client, user: User) -> None:
    """What the edit form does: one PATCH with all three fields, and the response already
    reports the version the database gave it."""
    with acting_as(user):
        note = create_note_for(user, title="Draft", body="rough", tags="idea")

    edited = auth_client.patch(
        f"/api/notes/{note.pk}",
        {"title": "Draft v2", "body": "less rough", "tags": "idea, done"},
        content_type="application/json",
    )

    assert edited.status_code == 200
    body = edited.json()
    assert (body["title"], body["body"], body["tags"]) == (
        "Draft v2",
        "less rough",
        "done, idea",
    )
    assert body["version"] == 2


def test_the_edit_shows_up_in_the_notes_history(auth_client: Client, user: User) -> None:
    with acting_as(user):
        note = create_note_for(user, title="Draft", tags="idea")
    auth_client.patch(
        f"/api/notes/{note.pk}", {"tags": "idea, done"}, content_type="application/json"
    )

    history = auth_client.get(f"/api/history/note/{note.pk}").json()

    newest = history["groups"][0]["revisions"][0]
    assert newest["changes"] == [{"field": "tags", "old": "idea", "new": "done, idea"}]


@pytest.mark.parametrize(
    ("written", "stored"),
    [
        ("  walk ,birds,  ", "birds, walk"),  # trimmed, empties dropped
        ("Birds, birds, BIRDS", "Birds"),  # deduplicated case-insensitively, first spelling wins
        ("b, a", "a, b"),  # sorted, so reordering is not a change
        ("", ""),
        (",,,", ""),
    ],
)
def test_tags_are_normalised(user: User, written: str, stored: str) -> None:
    with acting_as(user):
        note = create_note_for(user, title="x", tags=written)
    assert note.tags == stored


def test_reordering_tags_is_not_a_change(user: User) -> None:
    """Normalising on write is what keeps the version history honest: retyping the same set in
    a different order must not look like an edit."""
    with acting_as(user):
        note = create_note_for(user, title="x", tags="walk, birds")
        services.patch_note(user, NoteId(note.pk), NotePatch(tags="birds,walk"))
        note.refresh_from_db()

        assert note.tags == "birds, walk"
        # It is still a version — a save is a save — but nothing in it changed.
        (newest, _first) = revisions.revisions_of(note)
    assert newest.changes == []


def test_tag_list_splits_the_stored_string(user: User) -> None:
    with acting_as(user):
        note = create_note_for(user, title="x", tags="birds, walk")
    assert note.tag_list() == ["birds", "walk"]


def test_the_tags_field_did_not_exist_under_the_previous_schema_tag(user: User) -> None:
    """`tags` was added after 2026-08, so a row written then cannot speak for it: the revision
    page reports it as unknown rather than inventing an empty-to-something change."""
    older = history.fields_at("2026-08", Note._meta.label)
    current = history.fields_at(history.SCHEMA_TAG, Note._meta.label)

    assert older is not None and "tags" not in older
    assert current is not None and "tags" in current
