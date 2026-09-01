"""Notes: schemas, logic and the ninja router in one module.

**This app is the showcase for versioning and lineage.** Notes are edited, and every edit is
a version; then notes are *merged*, and the merge records which **version** of each source it
read:

    merge   many notes -> one note   one edge per source, each pinned to its own version

Merging alone is enough to build a real graph rather than a chain: a note can be merged into
several others (branching), and merged notes can be merged again (a diamond). That is what
`apps/core/lineage.py::graph` walks and what `/lineage/note/<id>` draws.

The edges deliberately do not follow later edits: change a source afterwards and the merged
note still records the version it actually read, while `stale_derivations(source)` lists the
notes that would have to be rebuilt.

Functions shared by several operations take the acting `user` and carry a `_for` suffix where a
route already owns the plain name (the route name is the OpenAPI operation id, `config/api.py`).
"""

import uuid
from collections.abc import Iterable, Sequence

from django.db.models import QuerySet
from django.http import HttpRequest
from ninja import Field, ModelSchema, Router, Status
from ninja.errors import HttpError
from ninja.pagination import PageNumberPagination, paginate

from apps.accounts.api import current_user
from apps.accounts.models import User
from apps.core import lineage
from apps.core.schemas import StrictSchema
from apps.notes.models import Note, NoteId

router = Router(tags=["notes"])

#: Room for a comma-separated list; matches `Note.tags`.
MAX_TAGS_LENGTH = 500
#: A merged note keeps its sources readable rather than silently concatenating them.
MERGE_SEPARATOR = "\n\n---\n\n"
MAX_MERGE_SOURCES = 20


class NoteOut(ModelSchema):
    id: uuid.UUID
    body: str
    tags: str = Field(description="Comma-separated, normalised on write")
    version: int

    class Meta:
        model = Note
        fields = ["id", "title", "body", "tags", "version", "created", "modified"]


class NoteIn(StrictSchema):
    """Create (POST) and full update (PUT)."""

    title: str = Field(min_length=1, max_length=200)
    body: str = ""
    tags: str = Field(default="", max_length=MAX_TAGS_LENGTH)


class NotePatch(StrictSchema):
    """Partial update (PATCH): only the fields that are present change."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = None
    tags: str | None = Field(default=None, max_length=MAX_TAGS_LENGTH)


class MergeNotesIn(StrictSchema):
    """Join several notes into a new one (`POST /api/notes/merge`)."""

    note_ids: list[uuid.UUID] = Field(min_length=2, max_length=20)
    title: str = Field(min_length=1, max_length=200)


def normalize_tags(tags: str) -> str:
    """Tidy a comma-separated tag list: trimmed, deduplicated case-insensitively, sorted.

    Normalising on write keeps the stored value canonical, which matters more than usual here:
    `tags` is a tracked field, so "a, b" and "b,  a" would otherwise show up as a change in the
    note's history without anything having actually changed.
    """
    seen: dict[str, str] = {}
    for part in tags.split(","):
        tag = part.strip()
        if tag:
            seen.setdefault(tag.casefold(), tag)
    return ", ".join(sorted(seen.values(), key=str.casefold))


def merge_tags(notes: Iterable[Note]) -> str:
    return normalize_tags(",".join(note.tags for note in notes))


def get_note_for(user: User, note_id: NoteId) -> Note:
    """One note, or a 404 — another user's note does not exist from here."""
    try:
        return Note.objects.for_user(user).get(pk=note_id)
    except Note.DoesNotExist:
        raise HttpError(404, "Note not found") from None


def create_note_for(user: User, *, title: str, body: str = "", tags: str = "") -> Note:
    return Note.objects.create(owner=user, title=title, body=body, tags=normalize_tags(tags))


def merge_notes_for(user: User, note_ids: Sequence[NoteId], *, title: str) -> Note:
    """Join several notes into a new one, with an edge to the current version of each source.

    The one derivation notes have, and the thing that makes their lineage a graph: the result
    has several parents, each pinned to the version that was actually read. The sources are
    left alone — merging adds a note, it does not consume any.
    """
    unique = list(dict.fromkeys(note_ids))
    if len(unique) < 2:
        raise HttpError(400, "Merging needs at least two different notes")
    if len(unique) > MAX_MERGE_SOURCES:
        raise HttpError(400, f"At most {MAX_MERGE_SOURCES} notes can be merged at once")

    sources = [get_note_for(user, note_id) for note_id in unique]
    body = MERGE_SEPARATOR.join(f"## {note.title}\n\n{note.body}".strip() for note in sources)
    merged = create_note_for(user, title=title, body=body, tags=merge_tags(sources))
    lineage.record_derivation(merged, sources=list(sources))
    return merged


# --- Endpoints ----------------------------------------------------------------------------------


@router.get("/notes", response=list[NoteOut])
@paginate(PageNumberPagination)
def list_notes(request: HttpRequest) -> QuerySet[Note]:
    return Note.objects.for_user(current_user(request))


@router.post("/notes", response={201: NoteOut})
def create_note(request: HttpRequest, payload: NoteIn) -> Status[Note]:
    note = create_note_for(
        current_user(request), title=payload.title, body=payload.body, tags=payload.tags
    )
    return Status(201, note)


@router.post("/notes/merge", response={201: NoteOut})
def merge_notes(request: HttpRequest, payload: MergeNotesIn) -> Status[Note]:
    """Join several notes into a new one, with an edge to the current version of each source.

    Registered before `/notes/{note_id}` so "merge" is not read as an id.
    """
    note = merge_notes_for(
        current_user(request),
        [NoteId(note_id) for note_id in payload.note_ids],
        title=payload.title,
    )
    return Status(201, note)


@router.get("/notes/{note_id}", response=NoteOut)
def get_note(request: HttpRequest, note_id: uuid.UUID) -> Note:
    return get_note_for(current_user(request), NoteId(note_id))


@router.put("/notes/{note_id}", response=NoteOut)
def update_note(request: HttpRequest, note_id: uuid.UUID, payload: NoteIn) -> Note:
    """PUT: replace every field with the payload (schema defaults for omitted ones)."""
    note = get_note_for(current_user(request), NoteId(note_id))
    note.set_payload(payload)
    note.tags = normalize_tags(note.tags)
    note.save()
    return note


@router.patch("/notes/{note_id}", response=NoteOut)
def patch_note(request: HttpRequest, note_id: uuid.UUID, payload: NotePatch) -> Note:
    """PATCH: change only the fields the client sent."""
    note = get_note_for(current_user(request), NoteId(note_id))
    note.set_payload_partial(payload)
    note.tags = normalize_tags(note.tags)
    note.save()
    return note


@router.delete("/notes/{note_id}", response={204: None})
def delete_note(request: HttpRequest, note_id: uuid.UUID) -> Status[None]:
    """Soft delete (apps/core/models.py): the row keeps its place in the version chain, and
    `objects` stops returning it — so a second delete is a 404 exactly as before.

    Notes merged from it keep their edges, and those still resolve: the source row is still
    there, one version further on. That is the reason deletes are soft.
    """
    get_note_for(current_user(request), NoteId(note_id)).soft_delete()
    return Status(204, None)
