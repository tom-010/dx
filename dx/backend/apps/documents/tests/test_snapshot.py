"""The snapshot contract, end to end, on the fake extraction fixture (`conftest.py`):
the acceptance tests of `documents_agent_brief.md` §9 plus the write-path rules of §4.
"""

from datetime import timedelta
from html.parser import HTMLParser

import pytest
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.core.testing import acting_as
from apps.documents import ops, snapshot, strategies
from apps.documents.api import search_documents_for
from apps.documents.extraction import ExtractedNode, ExtractedPage, ExtractedRegion, Extraction
from apps.documents.models import (
    Blob,
    ConfStats,
    Document,
    DocumentContent,
    ExtractionStatus,
    PageRegion,
    content_switched,
)
from apps.documents.tests.conftest import HEADING, STRADDLING, TRAILING, FakeStrategy, upload

pytestmark = pytest.mark.django_db


@pytest.fixture
def content(user: User, document: Document, fake: FakeStrategy) -> DocumentContent:
    with acting_as(user):
        return snapshot.extract_now(document, fake)


# --- §9.1 confidence -----------------------------------------------------------------------------


def test_confidence_rolls_up_from_words_at_every_level(
    user: User, content: DocumentContent
) -> None:
    with acting_as(user):
        assert snapshot.verify_snapshot(content) == []
        regions = list(PageRegion.objects.filter(node__content=content))
        every_word = [word.conf for region in regions for word in region.word_list()]
        assert content.conf_stats == ConfStats.of(every_word)
        assert content.conf_stats is not None and content.conf_stats.n == 8
        assert content.conf_stats == ConfStats.merge(r.conf_stats for r in regions)

        heading = content.node(2)
        assert heading.conf_stats == ConfStats.of([0.99, 0.95, 0.5])
        assert heading.conf_stats is not None and heading.conf_stats.min == 0.5
        assert content.node(1).conf_stats == content.conf_stats  # the section holds every word
        assert content.node(4).conf_stats is None  # a list without word boxes: no fake 1.0
        assert content.node(8).conf_stats is None  # detect_conf is not text confidence

        page1 = content.pages.get(number=1)
        assert page1.conf_stats == ConfStats.of([0.99, 0.95, 0.5, 0.9, 0.8, 0.7])
        assert content.pages.get(number=2).conf_stats == ConfStats.of([0.6, 0.4])


def _of(confs: list[float]) -> ConfStats:
    stats = ConfStats.of(confs)
    assert stats is not None
    return stats


def test_merging_summaries_equals_recomputing_them() -> None:
    left, right = [0.1, 0.55, 1.0], [0.0, 0.89, 0.9, 0.95]
    merged = ConfStats.merge([ConfStats.of(left), ConfStats.of(right), None])
    whole = _of(left + right)
    assert merged is not None
    assert (merged.n, merged.min, merged.max) == (whole.n, whole.min, whole.max)
    assert merged.hist == whole.hist
    assert merged.sum == pytest.approx(whole.sum)  # summed in another order
    assert merged.mean == pytest.approx(sum(left + right) / 7)
    assert _of([1.0]).hist == [0, 0, 0, 0, 0, 0, 0, 0, 0, 1]  # last bucket right-inclusive
    assert _of([0.0]).hist == [1, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    assert _of([0.89]).hist[8] == 1
    assert _of([0.9]).hist[9] == 1
    assert ConfStats.of([None, None]) is None
    assert ConfStats.merge([]) is None


# --- §9.2 offsets ---------------------------------------------------------------------------------


def test_offsets_are_codepoints_and_survive_astral_characters(
    user: User, content: DocumentContent
) -> None:
    with acting_as(user):
        heading, paragraph = content.node(2), content.node(3)
        assert heading.text() == HEADING  # holds an emoji: one codepoint, two UTF-16 units
        assert content.text[heading.text_start : heading.text_end] == HEADING
        assert paragraph.text_start == len(HEADING) + 2  # "\n\n" between leaves
        assert paragraph.text() == STRADDLING
        assert content.node(10).text() == TRAILING
        assert content.text.endswith(TRAILING)


# --- §9.3 html slices ----------------------------------------------------------------------------


class _Structure(HTMLParser):
    """The data-nid attributes of a fragment, in order, and whether its tags balance."""

    def __init__(self) -> None:
        super().__init__()
        self.nids: list[int] = []
        self.depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.depth += 1
        for key, value in attrs:
            if key == "data-nid" and value:
                self.nids.append(int(value))

    def handle_endtag(self, tag: str) -> None:
        self.depth -= 1


def test_node_html_is_the_elements_outer_html(user: User, content: DocumentContent) -> None:
    with acting_as(user):
        nodes = list(content.nodes.all())
        for node in nodes:
            piece = node.html()
            assert piece.startswith(f'<{node.tag} data-nid="{node.nid}"')
            assert piece.endswith(f"</{node.tag}>")
            structure = _Structure()
            structure.feed(piece)
            structure.close()
            assert structure.depth == 0, f"unbalanced slice for node #{node.nid}"
            assert structure.nids == [n.nid for n in node.subtree()]
        top = [n for n in nodes if n.parent_id is None]
        assert "".join(n.html() for n in top) == content.html
        # Text is escaped in the html and plain in the text projection.
        assert "&lt;special&gt;" in content.node(10).html()
        assert "<th>Item</th>" in content.node(7).html()
        assert "Food &amp; drink" in content.node(7).html()
        assert content.node(7).text() == "Item\tAmount\nRent\t1200\nFood & drink\t300"


def test_paths_nids_titles_and_levels(user: User, content: DocumentContent) -> None:
    with acting_as(user):
        rows = [(n.nid, n.path, n.tag, n.level, n.title) for n in content.nodes.all()]
    assert rows == [
        (1, "0001", "section", None, HEADING),
        (2, "0001.0001", "h1", 1, HEADING),
        (3, "0001.0002", "p", None, None),
        (4, "0001.0003", "ul", None, None),
        (5, "0001.0003.0001", "li", None, None),
        (6, "0001.0003.0002", "li", None, None),
        (7, "0001.0004", "table", None, None),
        (8, "0001.0005", "figure", None, None),
        (9, "0001.0005.0001", "figcaption", None, None),
        (10, "0002", "p", None, None),
    ]
    with acting_as(user):
        assert [n.nid for n in content.node(4).subtree()] == [4, 5, 6]
        assert content.node(6).ancestor_paths() == ["0001", "0001.0003"]
        assert [n.nid for n in content.outline()] == [2]


# --- §9.4 / §9.5 the current flip ----------------------------------------------------------------


def test_a_second_current_snapshot_is_refused_and_flips_leave_exactly_one(
    user: User, content: DocumentContent, fake: FakeStrategy
) -> None:
    document = content.document
    with acting_as(user):
        with pytest.raises(IntegrityError), transaction.atomic():
            DocumentContent.create(
                operation=None,
                sources=[],
                document=document,
                blob=content.blob,
                extractor=content.extractor,
                status=ExtractionStatus.SUCCEEDED,
                is_current=True,
            )
        second = snapshot.extract_now(document, fake)
        assert second.pk != content.pk and second.is_current
        assert DocumentContent.objects.filter(document=document, is_current=True).count() == 1
        assert not DocumentContent.objects.get(pk=content.pk).is_current
        assert Document.objects.get(pk=document.pk).current_content_id == second.pk
        assert document.contents.count() == 2  # history: both snapshots stay


def test_the_flip_signals_once_and_is_atomic(
    user: User, content: DocumentContent, fake: FakeStrategy
) -> None:
    document = content.document
    seen: list[tuple[object, object]] = []

    def receiver(sender: object, **kwargs: object) -> None:
        previous = kwargs["previous"]
        seen.append((kwargs["content"], getattr(previous, "pk", None)))

    content_switched.connect(receiver)
    try:
        with acting_as(user):
            second = snapshot.extract_now(document, fake)
    finally:
        content_switched.disconnect(receiver)
    assert [(c.pk, p) for c, p in seen] == [(second.pk, content.pk)]  # type: ignore[attr-defined]

    def boom(sender: object, **kwargs: object) -> None:
        raise RuntimeError("search index is down")

    content_switched.connect(boom)
    try:
        with acting_as(user):
            third = DocumentContent.create(
                operation=None,
                sources=[],
                document=document,
                blob=content.blob,
                extractor=content.extractor,
                status=ExtractionStatus.SUCCEEDED,
            )
            with pytest.raises(RuntimeError, match="search index"):
                snapshot.switch_current(document, third)
            # Nothing moved: the receiver ran inside the flip's transaction.
            assert Document.objects.get(pk=document.pk).current_content_id == second.pk
            assert not DocumentContent.objects.get(pk=third.pk).is_current
    finally:
        content_switched.disconnect(boom)


def test_extraction_records_its_lineage(user: User, content: DocumentContent) -> None:
    with acting_as(user):
        sources = content.sources()
        assert {v.model for v in sources} == {Blob, type(content.extractor)}
        document = Document.objects.get(pk=content.document_id)
        assert [v.model for v in document.sources()] == [DocumentContent]
        assert document.version == 2  # created, then pointed at the snapshot


# --- §9.6 hit testing -----------------------------------------------------------------------------


def test_hit_prefers_the_shape_over_the_envelope_and_the_smallest_area(
    user: User, content: DocumentContent
) -> None:
    with acting_as(user):
        page = content.pages.get(number=2)
        alpha, beta, items = content.node(5), content.node(6), content.node(4)
        assert page.hit(0.5, 0.7) == alpha  # the diamond's centre
        assert page.hit(0.33, 0.53) == beta  # in the diamond's envelope, outside its shape
        assert page.hit(0.41, 0.61) == beta  # in both: the smaller area wins
        assert page.hit(0.2, 0.4) == items  # only the list's own box there
        assert page.hit(0.95, 0.5) is None  # page furniture / empty space
        assert content.document.hit(2, 0.5, 0.7) == alpha  # through the facade
        assert content.document.hit(9, 0.5, 0.7) is None

        word = page.hit_word(0.2, 0.1)
        assert word is not None
        assert (word.node.nid, word.text, word.word.conf) == (3, "two", 0.6)
        assert page.hit_word(0.5, 0.7) is None  # a region without word boxes


# --- §9.7 reduced html ----------------------------------------------------------------------------


def test_reduced_html_deduplicates_ancestors_and_repeats_straddling_nodes(
    user: User, content: DocumentContent
) -> None:
    with acting_as(user):
        page1, page2 = content.pages.get(number=1), content.pages.get(number=2)
        assert [n.nid for n in page1.reduced_nodes()] == [2, 3]
        # The list is drawn on page 2, so its items are not listed a second time; the
        # straddling paragraph appears in full on both pages.
        assert [n.nid for n in page2.reduced_nodes()] == [3, 4, 7, 8]
        assert page1.reduced_html() == content.node(2).html() + content.node(3).html()
        assert page2.reduced_html().startswith(content.node(3).html())
        assert STRADDLING in page1.text() and STRADDLING in page2.text()


# --- §9.8 empty snapshots -------------------------------------------------------------------------


class EmptyStrategy(strategies.TreeStrategy):
    name = "empty"
    tool_version = "1"

    def parse(self, data: bytes, mime_type: str) -> Extraction:
        return Extraction(nodes=[], pages=[ExtractedPage(number=1)])


def test_zero_page_and_zero_node_snapshots_behave(user: User) -> None:
    fresh = upload(user, "notes.bin", b"\x00", "application/octet-stream")
    with acting_as(user):
        # No strategy for the type: the facade is empty, nothing raises.
        assert fresh.reextract() is None
        assert (fresh.text, fresh.html, fresh.outline(), fresh.confidence()) == ("", "", [], None)
        assert list(fresh.pages) == [] and fresh.hit(1, 0.5, 0.5) is None
        assert fresh.heading_title() == "" and fresh.latest_content() is None

        html_source = upload(user, "page.html", b"<h1>Hi</h1><p>there</p>", "text/html")
        content = snapshot.extract_now(html_source)
        assert content.status == ExtractionStatus.SUCCEEDED
        assert content.pages.count() == 0 and content.nodes.count() == 2
        assert snapshot.verify_snapshot(content) == []
        assert html_source.heading_title() == "Hi"
        assert html_source.hit(1, 0.5, 0.5) is None and html_source.confidence() is None

        blank = snapshot.extract_now(upload(user, "blank.fake"), EmptyStrategy())
        assert blank.status == ExtractionStatus.SUCCEEDED
        assert (blank.html, blank.text, blank.outline()) == ("", "", [])
        page = blank.pages.get(number=1)
        assert page.reduced_html() == "" and page.text() == "" and page.hit(0.5, 0.5) is None
        assert snapshot.verify_snapshot(blank) == []


# --- §9.9 blobs -----------------------------------------------------------------------------------


def test_blobs_are_deduplicated_and_orphans_collected(user: User, media_root: object) -> None:
    from pathlib import Path

    root = Path(str(media_root))

    def stored() -> list[Path]:
        return sorted(p for p in root.rglob("*") if p.is_file())

    with acting_as(user):
        first = snapshot.store_blob(user.pk, ContentFile(b"same bytes", name="a.txt"), "text/plain")
        again = snapshot.store_blob(user.pk, ContentFile(b"same bytes", name="b.txt"), "text/plain")
        assert first.pk == again.pk and Blob.objects.count() == 1
        assert len(stored()) == 1
        sha = first.sha256
        assert str(first.file.name) == f"documents/{user.pk}/blobs/{sha[:2]}/{sha[2:4]}/{sha}"

        loose = snapshot.store_bytes(user.pk, b"other bytes", "text/plain")
        kept = upload(user).source_blob
        assert [b.pk for b in ops.orphan_blobs()] == [first.pk, loose.pk]
        assert ops.gc_blobs(ops.orphan_blobs()) == 2
        assert list(Blob.objects.all()) == [kept]
        assert len(stored()) == 3  # objects are only reclaimed by tenant erasure

        # The same bytes again: a new row, but the object that is already there is reused.
        third = snapshot.store_blob(user.pk, ContentFile(b"same bytes", name="c.txt"), "text/plain")
        assert third.pk != first.pk and third.file.name == first.file.name
        assert len(stored()) == 3


# --- §9.11 cross-page paragraphs ------------------------------------------------------------------


def test_a_cross_page_paragraph_has_two_regions_and_lists_both_pages(
    user: User, content: DocumentContent
) -> None:
    with acting_as(user):
        paragraph = content.node(3)
        polygons = paragraph.polygons()
        assert [p.page.number for p in polygons] == [1, 2]
        assert polygons[0].points == [(0.1, 0.2), (0.9, 0.2), (0.9, 0.5), (0.1, 0.5)]
        assert paragraph.pages() == [1, 2]
        assert 'data-pages="1,2"' in paragraph.html()
        assert 'data-pages="1,2"' in content.node(1).html()
        assert 'data-pages="2"' in content.node(4).html()
        assert "data-pages" not in content.node(10).html()
        regions = list(paragraph.regions.all())
        assert [r.text() for r in regions] == ["First paragraph spanning ", "two pages"]
        # Scaled to a render of the page's own size: pixels.
        x, y = paragraph.polygons(612, 792)[0].points[0]
        assert (round(x, 6), round(y, 6)) == (61.2, 158.4)


# --- §9.12 search ---------------------------------------------------------------------------------


def test_search_hits_resolve_to_the_deepest_node(user: User, content: DocumentContent) -> None:
    with acting_as(user):
        (hit,) = search_documents_for(user, "beta")
        assert hit.document.pk == content.document_id
        assert hit.node is not None and hit.node.nid == 6  # the item, not the list or section
        assert "Beta" in hit.snippet
        (hit,) = search_documents_for(user, "annual report")
        assert hit.node is not None and hit.node.nid == 2
        assert search_documents_for(user, "nowhere") == []
        assert search_documents_for(user, "   ") == []
        assert content.node_at(content.node(3).text_start) == content.node(3)
        assert content.node_at(len(content.text) + 5) is None


# --- §9.13 sanitizer ------------------------------------------------------------------------------


def test_disallowed_markup_is_stripped_before_offsets_are_measured(
    user: User, fake: FakeStrategy, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = snapshot.render

    def tainted(nodes: list[snapshot._Planned]) -> str:
        html = original(nodes).replace('<p data-nid="10"', '<p data-nid="10" onclick="x()"')
        return html + '<script>alert(1)</script><b>bold</b><img src="x">'

    monkeypatch.setattr(snapshot, "render", tainted)
    with acting_as(user):
        content = snapshot.extract_now(upload(user), fake)
        assert content.status == ExtractionStatus.SUCCEEDED, content.error
        assert "onclick" not in content.html and "<script" not in content.html
        assert "<img" not in content.html and content.html.endswith("bold")
        assert snapshot.verify_snapshot(content) == []


def test_sanitize_keeps_the_vocabulary_and_nothing_else() -> None:
    dirty = '<p data-nid="1" data-pages="3" id="x"><em>a</em> &amp; b</p><style>p{}</style>'
    assert snapshot.sanitize(dirty) == '<p data-nid="1" data-pages="3">a &amp; b</p>'
    table = '<table><tbody><tr><td colspan="2" rowspan="1" style="">c</td></tr></tbody></table>'
    assert snapshot.sanitize(table) == table.replace(' style=""', "")
    assert snapshot.locate('<p data-nid="7">x</p>\n<p data-nid="8">y</p>') == {
        7: (0, 21),
        8: (22, 43),
    }


# --- §9.14 pruning --------------------------------------------------------------------------------


def test_prune_spares_the_current_snapshot_and_the_facade_survives_a_null_pointer(
    user: User, content: DocumentContent, fake: FakeStrategy
) -> None:
    document = content.document
    with acting_as(user):
        current = snapshot.extract_now(document, fake)
        old = [
            c.pk for c in ops.prunable_contents(older_than_days=0, keep_latest_per_extractor=False)
        ]
        assert old == [content.pk]
        assert list(ops.prunable_contents(older_than_days=0, keep_latest_per_extractor=True)) == [
            content
        ]
        with pytest.raises(ValueError, match="current"):
            ops.prune_contents([current])

        counts = ops.prune_contents(DocumentContent.objects.filter(pk=content.pk))
        assert counts == {"contents": 1, "pages": 2, "nodes": 10, "regions": 8}
        assert not DocumentContent.objects.filter(pk=content.pk).exists()
        assert DocumentContent.all_objects.get(pk=content.pk).deleted_at is not None
        assert content.nodes.count() == 0 and content.pages.count() == 0
        document = Document.objects.get(pk=document.pk)
        assert document.current_content_id == current.pk and document.html != ""

        document.current_content = None
        document.save(operation=None, sources=[])
        assert (document.text, document.html, document.outline(), list(document.pages)) == (
            "",
            "",
            [],
            [],
        )
        assert document.confidence() is None and document.hit(1, 0.5, 0.5) is None


# --- §4 the write path ----------------------------------------------------------------------------


def test_reextract_queues_one_pending_run_per_input(user: User, document: Document) -> None:
    with acting_as(user):
        queued = document.reextract()
        assert queued is not None and queued.status == ExtractionStatus.PENDING
        assert document.reextract() == queued  # an identical run is already queued
        assert document.contents.count() == 1
        done = snapshot.run_extraction(queued.pk)
        assert done.pk == queued.pk and done.status == ExtractionStatus.SUCCEEDED
        assert done.is_current and done.started_at is not None and done.finished_at is not None
        assert done.stats["nodes"] == 10 and done.stats["failed_pages"] == []
        assert done.raw_output is not None and done.raw_output.read_bytes() == b'{"fake": true}'
        stored = Document.objects.get(pk=document.pk)
        assert stored.meta == {"title": "Annual report", "filename": "report.fake"}
        assert snapshot.run_extraction(queued.pk) == done  # a redelivered task changes nothing
        again = document.reextract()
        assert again is not None and again.pk != queued.pk


def test_a_run_whose_worker_is_gone_is_taken_over_by_the_next_one(
    user: User, document: Document, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker restarted mid-run leaves its row RUNNING for ever. Without this, "Read again"
    would hand that dead row back and the document could never be read at all."""
    with acting_as(user):
        queued = document.reextract()
        assert queued is not None
        queued.status = ExtractionStatus.RUNNING
        queued.started_at = timezone.now()
        queued.save(operation=None, sources=[], update_fields=["status", "started_at"])
        # A run that is still working is left alone, however long it has been going.
        assert not snapshot.is_stale(queued)
        assert document.reextract() == queued

        # …and one nothing has touched since before the cutoff is closed and replaced. The
        # cutoff is moved rather than the row: `modified` is set by the database trigger on
        # every write, which is exactly what makes it a heartbeat and what makes it unfakeable.
        monkeypatch.setattr(snapshot, "STALE_RUN", timedelta(0))
        assert snapshot.is_stale(queued)
        taken_over = document.reextract()
        assert taken_over is not None and taken_over.pk != queued.pk
        assert taken_over.status == ExtractionStatus.PENDING
        abandoned = DocumentContent.objects.get(pk=queued.pk)
        assert abandoned.status == ExtractionStatus.FAILED
        assert abandoned.error == "the worker did not finish this run"
        # A PENDING row is never taken over, however old: it may simply be waiting behind a
        # long job, and queueing a second one would pay for the same document twice.
        assert not snapshot.is_stale(taken_over)
        assert document.reextract() == taken_over


class BrokenStrategy(strategies.TreeStrategy):
    name = "broken"
    tool_version = "1"

    def parse(self, data: bytes, mime_type: str) -> Extraction:
        raise ValueError("cannot read this")


class HandRolledStrategy(strategies.ExtractionStrategy):
    """Builds its snapshot without `self.snapshot()`: the queued placeholder is superseded."""

    name = "hand-rolled"
    tool_version = "1"

    def read_file(self, data: bytes, mime_type: str) -> Extraction:
        """Every strategy can read bytes, even one that writes its own row — that is what
        makes it runnable by `manage.py extract`."""
        return Extraction(nodes=[ExtractedNode(tag="p", text="by hand")])

    def extract(self, document: Document) -> DocumentContent:
        content = DocumentContent.create(
            operation=None,
            sources=[],
            document=document,
            blob=document.source_blob,
            extractor=snapshot.extractor_row(self),
            status=ExtractionStatus.SUCCEEDED,
            html='<p data-nid="1">by hand</p>',
            text="by hand",
        )
        return content


def test_a_failing_strategy_is_recorded_not_raised(user: User, document: Document) -> None:
    with acting_as(user):
        failed = snapshot.extract_now(document, BrokenStrategy())
        assert failed.status == ExtractionStatus.FAILED
        assert failed.error == "ValueError: cannot read this"
        assert not failed.is_current and failed.finished_at is not None
        assert Document.objects.get(pk=document.pk).current_content is None
        assert failed.nodes.count() == 0  # no child rows required for a failure


class UnbuildableStrategy(strategies.TreeStrategy):
    """Reads the file fine, then hands the builder a tree it cannot accept."""

    name = "unbuildable"
    tool_version = "1"

    def parse(self, data: bytes, mime_type: str) -> Extraction:
        return Extraction(
            nodes=[
                ExtractedNode(
                    tag="p", text="x", regions=[ExtractedRegion(page=9, envelope=(0, 0, 1, 1))]
                )
            ],
            pages=[ExtractedPage(number=1)],
            raw=b'{"pages": []}',
            raw_mime="application/json",
        )


def test_a_run_that_fails_in_the_builder_keeps_what_the_extractor_produced(
    user: User, document: Document
) -> None:
    """An OCR read costs money and a bug in the builder must not throw it away: the raw output
    is on the row before the snapshot is built, so a rebuild can start from it."""
    with acting_as(user):
        failed = snapshot.extract_now(document, UnbuildableStrategy())
        assert failed.status == ExtractionStatus.FAILED
        assert failed.raw_output is not None
        assert failed.raw_output.read_bytes() == b'{"pages": []}'
        # …which is what `start_extraction(from_raw=True)` rebuilds from.
        assert document.latest_content() == failed


def test_a_strategy_may_build_its_own_row(user: User, document: Document) -> None:
    with acting_as(user):
        result = snapshot.extract_now(document, HandRolledStrategy())
        assert result.is_current and result.text == "by hand"
        assert document.current_content == result
        # The queued placeholder `extract_now` made was superseded and retired.
        (placeholder,) = DocumentContent.all_objects.deleted().filter(document=document)
        assert placeholder.pk != result.pk
        assert document.contents.count() == 1


def test_a_partial_extraction_records_the_failed_pages(user: User) -> None:
    class Partial(strategies.TreeStrategy):
        name = "partial"
        tool_version = "1"

        def parse(self, data: bytes, mime_type: str) -> Extraction:
            return Extraction(
                nodes=[ExtractedNode(tag="p", text="page one")],
                pages=[ExtractedPage(number=1), ExtractedPage(number=2)],
                failed_pages=[2],
            )

    with acting_as(user):
        content = snapshot.extract_now(upload(user), Partial())
        assert content.status == ExtractionStatus.PARTIAL and content.is_current
        assert content.stats["failed_pages"] == [2]


def test_the_builder_refuses_trees_that_break_the_contract(user: User) -> None:
    bad_trees = {
        "vocabulary": Extraction(nodes=[ExtractedNode(tag="div", text="x")]),
        "leaf and container": Extraction(
            nodes=[ExtractedNode(tag="p", text="x", children=[ExtractedNode(tag="p", text="y")])]
        ),
        "missing page": Extraction(
            nodes=[
                ExtractedNode(
                    tag="p",
                    text="x",
                    regions=[ExtractedRegion(page=3, envelope=(0, 0, 1, 1))],
                )
            ]
        ),
        "span outside text": Extraction(
            nodes=[
                ExtractedNode(
                    tag="p",
                    text="x",
                    regions=[ExtractedRegion(page=1, envelope=(0, 0, 1, 1), span=(0, 9))],
                )
            ],
            pages=[ExtractedPage(number=1)],
        ),
        "coordinates": Extraction(
            nodes=[
                ExtractedNode(
                    tag="p",
                    text="x",
                    regions=[ExtractedRegion(page=1, envelope=(0, 0, 1.5, 1))],
                )
            ],
            pages=[ExtractedPage(number=1)],
        ),
    }
    for reason, tree in bad_trees.items():
        with pytest.raises(snapshot.SnapshotError):
            nodes, text = snapshot.plan(tree)
            snapshot.place(nodes, tree, text)
        del reason
