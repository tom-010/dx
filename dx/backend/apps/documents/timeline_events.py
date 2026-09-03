"""What the documents app puts on the timeline.

One file per app, by convention: `apps/timeline/apps.py` imports every `timeline_events.py`
at startup, which is what registers the types. The dependency points this way — documents
knows about the timeline, the timeline knows nothing about documents.

The event is *the document as it stands*, not a log line about the upload: `describe()` reads
the row every time, so re-recording after a rename or after the extraction finished updates
the card in place rather than adding a second one. `apps/documents/api.py` and
`apps/documents/snapshot.py` do exactly that at the three points where a document changes in a
way a reader would notice.
"""

from pydantic import BaseModel

from apps.documents.models import Document, Page
from apps.timeline.contracts import EventData, EventType, registry
from apps.timeline.models import DatePrecision, EventKind

#: The key `apps/documents/api.py` and `snapshot.py` pass to `timeline.record`. It lives here,
#: with the type that answers to it, so a rename is one edit.
DOCUMENT_UPLOADED = "documents.uploaded"


class DocumentUploadedPayload(BaseModel):
    """The extras a document card shows beyond title and description."""

    mime_type: str
    size: int
    page_count: int | None = None


@registry.register
class DocumentUploaded(EventType[Document]):
    key = DOCUMENT_UPLOADED
    kind = EventKind.TECHNICAL
    model = "documents.Document"
    label = "Document uploaded"
    description = "A file was added to the library."

    payload_schema = DocumentUploadedPayload

    def describe(self, obj: Document) -> EventData:
        current = obj.current_content
        pages = 0 if current is None else Page.objects.filter(content=current).count()
        return EventData(
            # When the file was uploaded. That is a technical event, so it is an instant, not
            # the date the *content* is from — `Document.date` answers that, and a real-world
            # event type built on it is what phase 2 adds.
            occurred_at=obj.created,
            date_precision=DatePrecision.DATETIME,
            # The title the document currently carries: the file name at upload, replaced by
            # what the document calls itself once the extraction has read it, unless a person
            # typed one (`snapshot.switch_current`).
            title=obj.title or str(obj.pk),
            description=_description(obj, pages),
            # Page one, rendered at row size — the same link the library list uses, so it is
            # already in the browser cache by the time the feed shows it. Empty where there is
            # no page to render.
            image_url=_thumb_url(obj, pages),
            payload=DocumentUploadedPayload(
                mime_type=obj.source_blob.mime_type,
                size=obj.source_blob.size,
                page_count=pages or None,
            ),
        )


def _description(document: Document, pages: int) -> str:
    """ "PDF, 3 pages" — the subtitle. Empty while nothing has been read yet: a card that says
    "0 pages" about a document still being extracted is worse than one that says nothing."""
    kind = document.source_blob.mime_type.rsplit("/", 1)[-1].upper()
    if pages == 0:
        return kind
    return f"{kind}, {pages} page{'s' if pages != 1 else ''}"


def _thumb_url(document: Document, pages: int) -> str:
    # Imported here, not at the top: `api` is what calls `record`, so importing it from the
    # module `api` imports would be a cycle. A PDF renders page one on demand; anything else
    # only has an image once a run stored one.
    from apps.documents.api import page_image_url  # noqa: PLC0415

    if document.source_blob.mime_type != "application/pdf" and pages == 0:
        return ""
    return page_image_url(document, 1, "row")
