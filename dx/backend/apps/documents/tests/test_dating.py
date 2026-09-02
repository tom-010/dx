"""Information-origin dating (Agent Brief 02 §7): the rule, EDTF ⇄ bounds, the diary
interpolation, the invariants, the period query, stamping, the facade, and re-dating."""

import json
from datetime import date

import pytest
from django.db import connection

from apps.accounts.models import User
from apps.core.examples import save_example
from apps.core.testing import acting_as
from apps.documents import dating, snapshot, strategies
from apps.documents.dating import DateSource, InvalidDate, UncertainDate, find_dateline
from apps.documents.extraction import ExtractedNode, ExtractedPage, ExtractedRegion, Extraction
from apps.documents.models import ConfStats, Dated, Document, DocumentContent, Page
from apps.documents.tests.conftest import FakeStrategy, upload

pytestmark = pytest.mark.django_db

MAY_12, MAY_20 = date(1943, 5, 12), date(1943, 5, 20)

#: A diary: a paragraph per page, datelines on pages 3 and 8, the first entry remembering 1918.
DIARY = {
    1: "Vorwort",
    2: "Meine Aufzeichnungen aus dem Krieg.",
    3: "12. Mai 1943\nHeute erinnere ich mich an den Sommer 1918, als alles begann.",
    4: "Regen den ganzen Tag.",
    5: "Wind von Westen.",
    6: "Sonne, endlich.",
    7: "Nebel am Morgen.",
    8: "20. Mai 1943\nBesuch von Anna.",
    9: "Nachwort",
}
#: A scrapbook: dated clippings out of order — reading order is not chronological order.
SCRAPBOOK = {1: "Fotos", 2: "1943", 3: "Ausschnitt", 4: "Notiz", 5: "12. Mai 1918", 6: "Brief"}


class PagesStrategy(strategies.TreeStrategy):
    """One paragraph per page, each placed on its page; the pages come from the raw output
    when rebuilding, so a rebuild never calls `parse()`."""

    name = "pages"
    tool_version = "1"
    texts: dict[int, str] = DIARY
    parsed = 0

    def parse(self, data: bytes, mime_type: str) -> Extraction:
        type(self).parsed += 1
        return self._tree(self.texts)

    def reproject(self, raw: bytes, mime_type: str) -> Extraction | None:
        return self._tree({int(k): str(v) for k, v in json.loads(raw).items()})

    @staticmethod
    def _tree(texts: dict[int, str]) -> Extraction:
        return Extraction(
            nodes=[
                ExtractedNode(
                    tag="p",
                    text=text,
                    regions=[
                        ExtractedRegion(
                            page=number, envelope=(0.1, 0.1, 0.9, 0.9), span=(0, len(text))
                        )
                    ],
                )
                for number, text in sorted(texts.items())
            ],
            pages=[ExtractedPage(number=number) for number in sorted(texts)],
            raw=json.dumps(texts).encode(),
        )


class ScrapbookStrategy(PagesStrategy):
    name = "scrapbook"
    texts = SCRAPBOOK


@pytest.fixture
def diary(user: User) -> DocumentContent:
    with acting_as(user):
        return snapshot.extract_now(upload(user, "diary.fake"), PagesStrategy())


def _page(content: DocumentContent, number: int) -> Page:
    return content.pages.get(number=number)


# --- §7.1 composition, not mention -------------------------------------------------------------


def test_the_date_is_when_the_entry_was_written_not_what_it_talks_about(
    user: User, diary: DocumentContent
) -> None:
    with acting_as(user):
        entry = diary.node(3)
        assert "1918" in entry.text()
        assert entry.date == UncertainDate("1943-05-12", MAY_12, MAY_12)
        assert entry.date_source == DateSource.EXPLICIT and entry.date_conf == 0.9
        rows: list[Dated] = [*diary.nodes.all(), *diary.pages.all()]
        years = {row.date_min.year for row in rows if row.date_min}
        years |= {row.date_max.year for row in rows if row.date_max}
        assert years == {1943}
        anchors = diary.stats["dating"]["anchors"]
        assert [a["edtf"] for a in anchors] == ["1943-05-12", "1943-05-20"]


def test_datelines_are_found_at_the_head_of_a_block_only() -> None:
    found = find_dateline("Montag, 12. Mai 1943\nLanger Text über 1918.")
    assert found is not None and (found.date.edtf, found.text) == ("1943-05-12", "12. Mai 1943")
    assert find_dateline("Wir sprachen lange über den Sommer 1918 und den Frühling 1919.") is None
    assert find_dateline("Berlin, den 3. März 1944") is not None
    assert find_dateline("May 12, 1943 — a note") is not None
    assert find_dateline("12th May 1943") is not None
    found = find_dateline("Mai 1943")
    assert found is not None and found.date.edtf == "1943-05" and found.conf == 0.8
    year = find_dateline("1943")
    assert year is not None and year.date.edtf == "1943" and year.precision == "year"
    assert find_dateline("Kapitel 1943 und danach") is None  # a bare year only stands alone
    impossible = find_dateline("31. Februar 1943")  # no such day: the month still dates it
    assert impossible is not None and impossible.date.edtf == "1943-02"
    assert find_dateline("Some Heading Written 12.5.1943", heading=True) is not None


# --- §7.2 EDTF ⇄ bounds ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "lower", "upper"),
    [
        ("1943", date(1943, 1, 1), date(1943, 12, 31)),
        ("1943-05", date(1943, 5, 1), date(1943, 5, 31)),
        ("1943-05-12", MAY_12, MAY_12),
        ("194X", date(1940, 1, 1), date(1949, 12, 31)),
        ("1943-XX", date(1943, 1, 1), date(1943, 12, 31)),
        ("1943?", date(1943, 1, 1), date(1943, 12, 31)),  # qualifiers stay in the string
        ("1943~", date(1943, 1, 1), date(1943, 12, 31)),
        ("1943-05-12/1943-05-20", MAY_12, MAY_20),
        ("../1943-05", None, date(1943, 5, 31)),
        ("1943-05/..", date(1943, 5, 1), None),
    ],
)
def test_edtf_derives_strict_bounds(text: str, lower: date | None, upper: date | None) -> None:
    parsed = UncertainDate.parse(text)
    assert (parsed.edtf, parsed.min, parsed.max) == (text, lower, upper)
    again = UncertainDate.from_bounds(lower, upper)
    assert (again.min, again.max) == (lower, upper)
    assert UncertainDate.parse(again.edtf) == again  # from_bounds picks a canonical form


def test_bounds_round_trip_to_the_shortest_edtf() -> None:
    assert UncertainDate.from_bounds(MAY_12, MAY_12).edtf == "1943-05-12"
    assert UncertainDate.from_bounds(date(1943, 5, 1), date(1943, 5, 31)).edtf == "1943-05"
    assert UncertainDate.from_bounds(date(1943, 1, 1), date(1943, 12, 31)).edtf == "1943"
    assert UncertainDate.from_bounds(date(1940, 1, 1), date(1949, 12, 31)).edtf == "194X"
    assert UncertainDate.from_bounds(MAY_12, MAY_20).edtf == "1943-05-12/1943-05-20"
    assert UncertainDate.from_bounds(None, MAY_12).edtf == "../1943-05-12"
    assert UncertainDate.from_bounds(MAY_20, None).edtf == "1943-05-20/.."
    assert UncertainDate.from_bounds(MAY_12, MAY_20).display() == "May 12–20, 1943"
    assert UncertainDate.parse("1943-05").display() == "May 1943"
    assert UncertainDate.parse("194X").display() == "1940s"
    assert UncertainDate.parse("../1943-05-12").display() == "on or before May 12, 1943"
    assert (
        UncertainDate.from_bounds(MAY_12, date(1944, 1, 3)).display()
        == "May 12, 1943 – Jan 3, 1944"
    )


@pytest.mark.parametrize(
    "text", ["nope", "1943-13", "1943-05-32", "0000", "1943-05-12T10:00", "", "../.."]
)
def test_invalid_edtf_is_rejected(text: str) -> None:
    with pytest.raises(InvalidDate):
        UncertainDate.parse(text)
    with pytest.raises(InvalidDate):
        UncertainDate.from_bounds(None, None)
    with pytest.raises(InvalidDate):
        UncertainDate.from_bounds(MAY_20, MAY_12)


def test_invalid_dates_are_rejected_at_write_time(user: User) -> None:
    with acting_as(user):
        page = save_example(Page.example())
        page.date_edtf = "nope"
        with pytest.raises(InvalidDate, match="not an EDTF date"):
            page.save(operation=None, sources=[])
        page.date_edtf = "1943-05"
        page.date_min, page.date_max = MAY_12, MAY_12  # not what the string says
        page.date_source = DateSource.EXPLICIT
        with pytest.raises(InvalidDate, match="bounds"):
            page.save(operation=None, sources=[])
        page.date_min, page.date_max = date(1943, 5, 1), date(1943, 5, 31)
        page.date_conf = 1.5
        with pytest.raises(InvalidDate, match="0..1"):
            page.save(operation=None, sources=[])
        page.date_conf = 0.5
        page.save(operation=None, sources=[])
        assert page.date is not None and page.date.display() == "May 1943"


def test_containment_and_envelopes_treat_a_null_bound_as_unknown() -> None:
    whole = UncertainDate.parse("1943-05-12/1943-05-20")
    before = UncertainDate.parse("../1943-05-12")
    after = UncertainDate.parse("1943-05-20/..")
    assert whole.contains(before) and whole.contains(after)
    assert not whole.contains(UncertainDate.parse("1943-05-21"))
    assert UncertainDate.envelope([before, whole, after]) == whole
    assert UncertainDate.envelope([before, after]) is None  # nothing known on either side agrees
    assert whole.overlaps(before) and not after.overlaps(before)
    assert whole.intersect(UncertainDate.parse("1943-05-15/..")) == UncertainDate.parse(
        "1943-05-15/1943-05-20"
    )
    assert whole.intersect(UncertainDate.parse("1944")) is None


# --- §7.3 / §7.4 the diary move ------------------------------------------------------------------


def test_diary_pages_between_anchors_are_interpolated(user: User, diary: DocumentContent) -> None:
    with acting_as(user):
        dates = {p.number: (p.date_edtf, p.date_source, p.date_conf) for p in diary.pages.all()}
    assert dates == {
        1: ("../1943-05-12", DateSource.INTERPOLATED, 0.4),
        2: ("../1943-05-12", DateSource.INTERPOLATED, 0.5),
        3: ("1943-05-12", DateSource.AGGREGATED, 0.9),
        4: ("1943-05-12/1943-05-20", DateSource.INTERPOLATED, 0.7),
        5: ("1943-05-12/1943-05-20", DateSource.INTERPOLATED, 0.6),
        6: ("1943-05-12/1943-05-20", DateSource.INTERPOLATED, 0.6),
        7: ("1943-05-12/1943-05-20", DateSource.INTERPOLATED, 0.7),
        8: ("1943-05-20", DateSource.AGGREGATED, 0.9),
        9: ("1943-05-20/..", DateSource.INTERPOLATED, 0.5),
    }
    with acting_as(user):
        assert diary.date == UncertainDate("1943-05-12/1943-05-20", MAY_12, MAY_20)
        assert diary.date_source == DateSource.AGGREGATED
        undated_page_node = diary.node(5)
        assert undated_page_node.date_source == DateSource.INHERITED
        assert undated_page_node.date_edtf == "1943-05-12/1943-05-20"
        assert undated_page_node.date_conf == 0.54  # the page's 0.6, one step less sure
        assert diary.stats["dating"]["interpolation"] == "applied to 7 page(s)"


def test_a_scrapbook_is_not_interpolated(user: User) -> None:
    with acting_as(user):
        content = snapshot.extract_now(upload(user, "scrapbook.fake"), ScrapbookStrategy())
        assert content.status == "succeeded", content.error
        dates = {p.number: p.date_edtf for p in content.pages.all()}
        assert dates == {1: None, 2: "1943", 3: None, 4: None, 5: "1918-05-12", 6: None}
        assert content.stats["dating"]["interpolation"].startswith(
            "skipped: anchors are not in chronological order"
        )
        assert content.date is not None and content.date.edtf == "1918-05-12/1943-12-31"
        assert [n.date_edtf for n in content.nodes.all()] == [
            None,
            "1943",
            None,
            None,
            "1918-05-12",
            None,
        ]
        assert snapshot.verify_snapshot(content) == []


def test_a_hint_narrows_the_open_ends_and_stands_in_when_nothing_is_dated(user: User) -> None:
    with acting_as(user):
        document = upload(user, "hinted.fake")
        document.meta = {"date_hint": "1943"}
        document.save(operation=None, sources=[])
        content = snapshot.extract_now(document, PagesStrategy())
        assert _page(content, 1).date_edtf == "1943-01-01/1943-05-12"
        assert _page(content, 9).date_edtf == "1943-05-20/1943-12-31"
        assert content.date is not None and content.date.edtf == "1943"

        blank = upload(user, "blank.fake")
        blank.meta = {"date_hint": "194X"}
        blank.save(operation=None, sources=[])
        content = snapshot.extract_now(blank, FakeStrategy())  # no datelines in the fixture
        assert (content.date_edtf, content.date_source, content.date_conf) == (
            "194X",
            DateSource.INFERRED,
            0.5,
        )
        assert {p.date_source for p in content.pages.all()} == {DateSource.INHERITED}
        assert content.node(1).date_source == DateSource.INHERITED
        assert snapshot.verify_snapshot(content) == []


# --- §7.5 invariants and immutability ----------------------------------------------------------


def test_dates_are_contained_tight_and_never_updated(user: User, diary: DocumentContent) -> None:
    with acting_as(user):
        assert snapshot.verify_snapshot(diary) == []
        assert dating.check_dating([], [], None) == []
        rows = [*diary.pages.all(), *diary.nodes.all()]
        assert {row.version for row in rows} == {1}  # written once, never touched again
        for node in diary.nodes.all():
            assert diary.date is not None and diary.date.contains(node.date or diary.date)


# --- §7.6 the period query ------------------------------------------------------------------------


def test_overlapping_handles_open_sides_and_ignores_undated_rows(
    user: User, diary: DocumentContent
) -> None:
    with acting_as(user):
        pages = diary.pages
        mid = sorted(
            p.number for p in pages.overlapping(UncertainDate.parse("1943-05-15/1943-05-16"))
        )
        assert mid == [4, 5, 6, 7]
        assert sorted(
            p.number for p in pages.overlapping(UncertainDate.parse("../1943-05-01"))
        ) == [1, 2]
        assert sorted(p.number for p in pages.overlapping(UncertainDate.parse("1943-06"))) == [9]
        assert sorted(p.number for p in pages.overlapping(UncertainDate.parse("1943-05-12"))) == [
            1,
            2,
            3,
            4,
            5,
            6,
            7,
        ]
        assert diary.nodes.overlapping(UncertainDate.parse("1943-05-20/..")).count() == 6

        scrapbook = snapshot.extract_now(upload(user, "scrapbook.fake"), ScrapbookStrategy())
        assert [p.number for p in scrapbook.pages.overlapping(UncertainDate.parse("1943"))] == [2]
        assert [
            p.number for p in scrapbook.pages.overlapping(UncertainDate.parse("1900/1950"))
        ] == [2, 5]
        assert scrapbook.pages.undated().count() == 4

        corpus = DocumentContent.objects.filter(is_current=True).overlapping(
            UncertainDate.parse("1943-05")
        )
        assert {c.pk for c in corpus} == {diary.pk, scrapbook.pk}
        assert list(
            Document.objects.filter(current_content__in=corpus.filter(date_min__gte=MAY_12))
        ) == [diary.document]


def test_the_corpus_query_is_served_by_an_index(user: User, diary: DocumentContent) -> None:
    """The partial index exists with its predicate, and the corpus query never scans the
    table. Which index the planner takes it cannot promise: the `NULL OR <=` predicate of
    `overlapping()` is not a btree condition, so under row-level security the tenant index
    and the (owner-leading) date index cost the same on a small tenant. Escape hatch if a
    tenant ever holds enough documents: an expression index on
    `COALESCE(date_min, '-infinity'), COALESCE(date_max, 'infinity')` and the same
    expressions in the query."""
    with acting_as(user):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT indexdef FROM pg_indexes WHERE indexname = 'documents_content_dates_idx'"
            )
            (definition,) = cursor.fetchone() or (None,)
        assert definition is not None
        assert "(owner_id, date_min, date_max)" in definition and "WHERE is_current" in definition
        query = (
            DocumentContent.objects.filter(is_current=True)
            .overlapping(UncertainDate.parse("1943-05"))
            .values("pk")
        )
        sql, params = query.query.sql_with_params()
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL enable_seqscan = off")  # a handful of rows would seq-scan
            cursor.execute("EXPLAIN " + sql, params)
            plan = "\n".join(str(row[0]) for row in cursor.fetchall())
            cursor.execute("SET LOCAL enable_seqscan = on")
    assert "Index Scan using documents_" in plan and "Seq Scan" not in plan, plan


# --- §7.7 stamping --------------------------------------------------------------------------------


def test_dated_tags_carry_data_date_and_undated_ones_do_not(
    user: User, diary: DocumentContent
) -> None:
    with acting_as(user):
        expected = (
            '<p data-nid="3" data-pages="3" data-date="1943-05-12">12. Mai 1943\n'
            "Heute erinnere ich mich an den Sommer 1918, als alles begann.</p>"
        )
        assert diary.node(3).html() == expected
        assert 'data-date="1943-05-12/1943-05-20"' in diary.node(5).html()
        scrapbook = snapshot.extract_now(upload(user, "scrapbook.fake"), ScrapbookStrategy())
        assert "data-date" not in scrapbook.node(1).html()
        assert 'data-date="1943"' in scrapbook.node(2).html()
    kept = snapshot.sanitize('<p data-nid="1" data-date="1943-05" data-when="x">a</p>')
    assert kept == '<p data-nid="1" data-date="1943-05">a</p>'


# --- §7.8 date_conf stays out of conf_stats ------------------------------------------------------


def test_date_conf_never_enters_conf_stats(
    user: User, diary: DocumentContent, fake: FakeStrategy
) -> None:
    with acting_as(user):
        assert (diary.date_conf, diary.conf_stats) == (0.4, None)  # born-digital: no OCR
        assert [(p.conf_stats, p.date_conf is not None) for p in diary.pages.all()] == [
            (None, True)
        ] * 9
        content = snapshot.extract_now(upload(user), fake)
        assert content.conf_stats == ConfStats.of([0.99, 0.95, 0.5, 0.9, 0.8, 0.7, 0.6, 0.4])
        assert content.date is None  # nothing in the fixture is a dateline
    assert (
        dating.aggregate(
            [dating.DateEstimate(UncertainDate.parse("1943"), DateSource.EXPLICIT, 0.9)]
        )
        is not None
    )


# --- §7.9 the facade ------------------------------------------------------------------------------


def test_an_undated_or_running_document_has_no_date_and_an_empty_timeline(
    user: User, fake: FakeStrategy
) -> None:
    fresh = upload(user)
    with acting_as(user):
        assert fresh.date is None
        assert list(fresh.timeline()) == []
        queued = fresh.reextract()
        assert queued is not None
        assert fresh.date is None
        assert list(fresh.timeline()) == []
        snapshot.run_extraction(queued.pk)
        fresh.refresh_from_db()
        assert fresh.date is None  # extracted, nothing dated
        assert list(fresh.timeline()) == []


def test_the_timeline_lists_dated_nodes_earliest_first(user: User, diary: DocumentContent) -> None:
    with acting_as(user):
        document = Document.objects.get(pk=diary.document_id)
        assert document.date is not None and document.date.display() == "May 12–20, 1943"
        timeline = list(document.timeline())
        assert [n.nid for n in timeline] == [3, 4, 5, 6, 7, 8, 9, 1, 2]  # open starts last
        assert [n.nid for n in document.timeline().filter(date_source=DateSource.EXPLICIT)] == [
            3,
            8,
        ]
        assert timeline[0].estimate is not None
        assert timeline[0].estimate.display() == "May 12, 1943 (explicit, 0.90)"


# --- §7.10 re-dating ------------------------------------------------------------------------------


def test_redating_rebuilds_from_the_raw_output_and_flips(
    user: User, diary: DocumentContent, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = diary.document
    parsed_before = PagesStrategy.parsed
    better = dating.Dateline(UncertainDate.parse("1943-05-15"), "stub", "day", 0.99)
    monkeypatch.setattr(
        dating, "find_dateline", lambda text, heading=False: better
    )  # "a better dating model"
    with acting_as(user):
        rebuilt = snapshot.extract_now(document, PagesStrategy(), from_raw=True)
        assert PagesStrategy.parsed == parsed_before  # reprojected: the extractor did not run
        assert rebuilt.pk != diary.pk and rebuilt.is_current
        assert rebuilt.date is not None and rebuilt.date.edtf == "1943-05-15"
        assert rebuilt.raw_output_id == diary.raw_output_id
        assert Document.objects.get(pk=document.pk).current_content_id == rebuilt.pk
        old = DocumentContent.objects.get(pk=diary.pk)
        assert not old.is_current and old.date_edtf == "1943-05-12/1943-05-20"
        assert {n.date_edtf for n in old.nodes.all()} == {
            "1943-05-12",
            "../1943-05-12",
            "1943-05-12/1943-05-20",
            "1943-05-20",
            "1943-05-20/..",
        }
        assert snapshot.verify_snapshot(rebuilt) == []

        with pytest.raises(snapshot.NothingToRebuildFrom):
            snapshot.extract_now(upload(user, "plain.fake"), FakeStrategy(), from_raw=True)


def test_rebuild_falls_back_to_parsing_when_the_strategy_cannot_reproject(
    user: User, fake: FakeStrategy
) -> None:
    with acting_as(user):
        first = snapshot.extract_now(upload(user), fake)
        assert first.raw_output is not None
        again = snapshot.extract_now(first.document, fake, from_raw=True)
        assert again.pk != first.pk and again.is_current and again.stats["nodes"] == 10
