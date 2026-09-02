"""The two housekeeping jobs behind `manage.py prune_contents` and `manage.py gc_blobs`.

History grows with every re-extraction by design; these are the counterweight. Both *retire*
rows rather than remove them: deletes are soft in this project and only tenant erasure
reclaims files (CLAUDE.md "Invariants", `docs/soft-delete.md`), so a pruned snapshot drops out
of every default query and its version history stays.
"""

from collections.abc import Iterable
from datetime import timedelta

from django.db import transaction
from django.db.models import OuterRef, Subquery
from django.utils import timezone

from apps.core.history import history_context
from apps.core.models import OwnedQuerySet
from apps.documents.models import (
    TERMINAL_STATUSES,
    Blob,
    Document,
    DocumentContent,
    ExtractionStatus,
    Node,
    Page,
    PageRegion,
)


def prunable_contents(
    *, older_than_days: int, keep_latest_per_extractor: bool
) -> OwnedQuerySet[DocumentContent]:
    """Non-current, terminal snapshots created more than N days ago — never the current one,
    and optionally never the latest successful run of each extractor on each document."""
    cutoff = timezone.now() - timedelta(days=older_than_days)
    found = DocumentContent.objects.filter(
        is_current=False, status__in=TERMINAL_STATUSES, created__lt=cutoff
    )
    if keep_latest_per_extractor:
        latest = (
            DocumentContent.objects.filter(
                document=OuterRef("document"),
                extractor=OuterRef("extractor"),
                status=ExtractionStatus.SUCCEEDED,
            )
            .order_by("-created")
            .values("pk")[:1]
        )
        found = found.exclude(pk=Subquery(latest))
    return found.order_by("created")


def prune_contents(contents: Iterable[DocumentContent]) -> dict[str, int]:
    """Retire the snapshots and their rows, in one transaction. Cascade is application logic
    here: regions, nodes and pages first, then the content rows. Refuses a current snapshot."""
    ids = [content.pk for content in contents]
    if not ids:
        return {}
    if DocumentContent.objects.filter(pk__in=ids, is_current=True).exists():
        raise ValueError("refusing to prune a current snapshot")
    with transaction.atomic(), history_context("prune snapshots"):
        regions, _ = PageRegion.objects.filter(node__content_id__in=ids).delete()
        nodes, _ = Node.objects.filter(content_id__in=ids).delete()
        pages, _ = Page.objects.filter(content_id__in=ids).delete()
        rows, _ = DocumentContent.objects.filter(pk__in=ids, is_current=False).delete()
    return {"contents": rows, "pages": pages, "nodes": nodes, "regions": regions}


def orphan_blobs() -> OwnedQuerySet[Blob]:
    """Blobs no foreign key points at — all five referencing columns, over every row of those
    tables including soft-deleted ones (`_base_manager`): a retired document still needs its
    bytes should it be restored."""
    return (
        Blob.objects.all()
        .exclude(pk__in=Document._base_manager.values("source_blob_id"))
        .exclude(pk__in=Document._base_manager.exclude(thumbnail=None).values("thumbnail_id"))
        .exclude(pk__in=DocumentContent._base_manager.values("blob_id"))
        .exclude(
            pk__in=DocumentContent._base_manager.exclude(raw_output=None).values("raw_output_id")
        )
        .exclude(pk__in=Page._base_manager.exclude(thumbnail=None).values("thumbnail_id"))
        .order_by("created")
    )


def gc_blobs(blobs: Iterable[Blob]) -> int:
    """Retire orphaned blob rows. The objects stay in the store: the rows' own history still
    names them, and files are reclaimed by tenant erasure only."""
    ids = [blob.pk for blob in blobs]
    if not ids:
        return 0
    with history_context("collect orphaned blobs"):
        count, _ = Blob.objects.filter(pk__in=ids).delete()
    return count
