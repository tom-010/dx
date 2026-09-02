"""Information-origin dating: from when the content of a snapshot originates.

**The rule this module enforces:** a snapshot's dates say when the content was *recorded or
composed*, never what dates the text talks about. A 1943 diary entry describing the events of
1918 is dated 1943. A date printed in the content itself — a diary entry's header, a letter's
dateline — is evidence, the strongest kind (an `EXPLICIT` estimate); a date mentioned in
running text is not extracted at all, and event timelines are a non-goal.

Representation — the house pattern, third instance after polygon → envelope and words →
conf_stats: `date_edtf` is the lossless truth in EDTF (ISO 8601-2, the Library of Congress
Extended Date/Time Format); `date_min` / `date_max` are its *strict* bounds, derived once at
write time for plain indexed SQL, a NULL bound meaning open on that side; `date_source` says
how we know (`DateSource`); `date_conf` is the estimator's belief in the attribution — a
per-estimate scalar like `PageRegion.detect_conf`, never merged into the additive OCR
`conf_stats`. `UncertainDate` is the one implementation of EDTF ⇄ bounds, containment and
display; `DatedQuerySet.overlapping()` (models.py) the one implementation of the NULL-bound
query.

The stage (`date_snapshot`) runs inside the snapshot builder after structure and text are
final and before the rows are written: datelines → page envelopes → interpolation over page
order → aggregation up the tree → the content envelope → inheritance down. A better dating
model means rebuilding the snapshot from `raw_output` (`snapshot.start_extraction(...,
from_raw=True)`) — no OCR cost, no second lifecycle.

Bounds are proleptic Gregorian `DateField`s (year ≥ 1) at day granularity: no BCE, no Julian
calendar, no time of day. EDTF can say more; the columns cannot, and `UncertainDate.parse`
refuses what they cannot hold.
"""

from __future__ import annotations

import calendar
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from time import struct_time
from typing import Protocol

from django.db import models
from edtf import parse_edtf
from edtf.parser.edtf_exceptions import EDTFParseException

EDTF_MAX_LENGTH = 64


class DateSource(models.TextChoices):
    """How a row's date is known — a column, because review queues filter on it."""

    #: A date printed in the content governs the row (a diary header, a letter's dateline).
    EXPLICIT = "explicit"
    #: File or container metadata (a PDF creation date) — a prior, often wrong for scans.
    METADATA = "metadata"
    #: A heuristic or model estimate from the content, without a dateline.
    INFERRED = "inferred"
    #: Undated, between dated anchors in reading order: the honest range [previous, next].
    INTERPOLATED = "interpolated"
    #: Copied top-down from the parent when nothing better exists.
    INHERITED = "inherited"
    #: The tight envelope over dated children, computed bottom-up.
    AGGREGATED = "aggregated"
    #: Set by a person — reserved; nothing writes it yet.
    CURATED = "curated"


class InvalidDate(ValueError):
    """Not an EDTF string the date columns can hold."""


# --- UncertainDate: EDTF ⇄ bounds --------------------------------------------------------------


@dataclass(frozen=True)
class UncertainDate:
    """An EDTF string and its strict bounds; `None` on a side means open there."""

    edtf: str
    min: date | None
    max: date | None

    @classmethod
    def parse(cls, text: str) -> UncertainDate:
        """Parse EDTF and derive the strict bounds; `InvalidDate` for anything else.

        Supported: `YYYY`, `YYYY-MM`, `YYYY-MM-DD`, the qualifiers `?` `~` `%` (kept in the
        string; the bounds stay strict), unspecified digits (`194X`, `1943-XX`), intervals
        `start/end` and the open forms `../end`, `start/..`.
        """
        cleaned = text.strip()
        if not cleaned or len(cleaned) > EDTF_MAX_LENGTH:
            raise InvalidDate(f"not an EDTF date: {text!r}")
        if "T" in cleaned:
            raise InvalidDate("time of day is not stored; give a date")
        try:
            parsed = parse_edtf(cleaned)
        except EDTFParseException as exc:
            raise InvalidDate(f"not an EDTF date: {cleaned!r}") from exc
        lower = _bound(parsed.lower_strict())
        upper = _bound(parsed.upper_strict())
        if lower is None and upper is None:
            raise InvalidDate(f"{cleaned!r} has no bound on either side")
        if lower is not None and upper is not None and lower > upper:
            raise InvalidDate(f"{cleaned!r} ends before it starts")
        return cls(edtf=cleaned, min=lower, max=upper)

    @classmethod
    def from_bounds(cls, lower: date | None, upper: date | None) -> UncertainDate:
        """The shortest EDTF string with exactly these strict bounds."""
        if lower is None and upper is None:
            raise InvalidDate("a date needs a bound on at least one side")
        if lower is not None and upper is not None and lower > upper:
            raise InvalidDate(f"{lower} is after {upper}")
        return cls(edtf=_edtf_for(lower, upper), min=lower, max=upper)

    @classmethod
    def envelope(cls, parts: Iterable[UncertainDate]) -> UncertainDate | None:
        """The tight envelope: MIN of the known lower bounds, MAX of the known upper ones.

        NULL-aware means a NULL bound is *unknown*, not infinite: it neither widens the
        envelope nor sets it (a side nobody knows stays NULL). Otherwise a diary whose first
        and last pages are undated — one-sided interpolations at both ends — would have no
        bound on either side, which the columns cannot hold and no reader wants. None when
        there are no parts, or when the known bounds contradict each other.
        """
        found = list(parts)
        if not found:
            return None
        lowers = [p.min for p in found if p.min is not None]
        uppers = [p.max for p in found if p.max is not None]
        lower = min(lowers) if lowers else None
        upper = max(uppers) if uppers else None
        if (lower is None and upper is None) or (lower and upper and lower > upper):
            return None
        return cls.from_bounds(lower, upper)

    @property
    def is_bounded(self) -> bool:
        return self.min is not None and self.max is not None

    def contains(self, other: UncertainDate) -> bool:
        """`other` lies within this range on every side where both bounds are known — a
        NULL bound is unknown and constrains nothing (the same reading as `envelope`)."""
        if self.min is not None and other.min is not None and other.min < self.min:
            return False
        if self.max is not None and other.max is not None and other.max > self.max:
            return False
        return True

    def overlaps(self, other: UncertainDate) -> bool:
        return (self.min is None or other.max is None or self.min <= other.max) and (
            self.max is None or other.min is None or self.max >= other.min
        )

    def intersect(self, other: UncertainDate) -> UncertainDate | None:
        """The common part of two ranges, or None when they are disjoint."""
        lowers = [v for v in (self.min, other.min) if v is not None]
        uppers = [v for v in (self.max, other.max) if v is not None]
        lower = max(lowers) if lowers else None
        upper = min(uppers) if uppers else None
        if lower is not None and upper is not None and lower > upper:
            return None
        if lower is None and upper is None:
            return None
        return UncertainDate.from_bounds(lower, upper)

    def display(self) -> str:
        """For a person: "May 12–20, 1943", "1940s", "on or before May 31, 1943"."""
        lower, upper = self.min, self.max
        if lower is None:
            return f"on or before {_long(upper)}" if upper is not None else "undated"
        if upper is None:
            return f"on or after {_long(lower)}"
        match _shape(lower, upper):
            case "day":
                return _long(lower)
            case "month":
                return f"{_month(lower)} {lower.year}"
            case "year":
                return str(lower.year)
            case "decade":
                return f"{lower.year}s"
            case _:
                pass
        if (lower.year, lower.month) == (upper.year, upper.month):
            return f"{_month(lower)} {lower.day}–{upper.day}, {lower.year}"
        if lower.year == upper.year:
            return f"{_month(lower)} {lower.day} – {_month(upper)} {upper.day}, {lower.year}"
        return f"{_long(lower)} – {_long(upper)}"

    def __str__(self) -> str:
        return self.display()


def _bound(value: object) -> date | None:
    """A strict bound from the parser: a `struct_time`, or ±inf for an open side."""
    if isinstance(value, struct_time):
        if value.tm_year < 1:
            raise InvalidDate("dates before year 1 cannot be stored (proleptic Gregorian)")
        return date(value.tm_year, value.tm_mon, value.tm_mday)
    return None  # -inf / inf


def _shape(lower: date, upper: date) -> str:
    if lower == upper:
        return "day"
    if (lower.year, lower.month, lower.day) == (upper.year, upper.month, 1) and (
        upper.day == calendar.monthrange(upper.year, upper.month)[1]
    ):
        return "month"
    if (lower.month, lower.day, upper.month, upper.day) == (1, 1, 12, 31):
        if lower.year == upper.year:
            return "year"
        if lower.year % 10 == 0 and upper.year == lower.year + 9:
            return "decade"
    return "interval"


def _edtf_for(lower: date | None, upper: date | None) -> str:
    if lower is None:
        assert upper is not None
        return f"../{upper.isoformat()}"
    if upper is None:
        return f"{lower.isoformat()}/.."
    match _shape(lower, upper):
        case "day":
            return lower.isoformat()
        case "month":
            return f"{lower.year:04d}-{lower.month:02d}"
        case "year":
            return f"{lower.year:04d}"
        case "decade":
            return f"{lower.year // 10:03d}X"
        case _:
            return f"{lower.isoformat()}/{upper.isoformat()}"


def _month(value: date) -> str:
    return calendar.month_abbr[value.month]


def _long(value: date) -> str:
    return f"{_month(value)} {value.day}, {value.year}"


# --- DateEstimate: a date, how it is known, and how sure --------------------------------------


@dataclass(frozen=True)
class DateEstimate:
    date: UncertainDate
    source: DateSource
    conf: float | None

    def display(self) -> str:
        """ "May 12–20, 1943 (interpolated, 0.60)" — from the stored fields alone."""
        conf = f", {self.conf:.2f}" if self.conf is not None else ""
        return f"{self.date.display()} ({self.source.value}{conf})"


def aggregate(parts: Sequence[DateEstimate]) -> DateEstimate | None:
    """The tight envelope of dated children; confidence is the weakest child's. None when
    the children's known bounds contradict each other (only one-sided ranges pointing
    away from each other)."""
    envelope = UncertainDate.envelope(part.date for part in parts)
    if envelope is None:
        return None
    confs = [part.conf for part in parts if part.conf is not None]
    return DateEstimate(envelope, DateSource.AGGREGATED, min(confs) if confs else None)


def inherit(estimate: DateEstimate, narrowed_to: UncertainDate | None = None) -> DateEstimate:
    """A copy of a parent's estimate for a child, one step less sure."""
    conf = round(estimate.conf * 0.9, 2) if estimate.conf is not None else None
    return DateEstimate(narrowed_to or estimate.date, DateSource.INHERITED, conf)


# --- Datelines: the explicit evidence -------------------------------------------------------------

_MONTHS = {
    "januar": 1,
    "january": 1,
    "jan": 1,
    "februar": 2,
    "february": 2,
    "feb": 2,
    "märz": 3,
    "maerz": 3,
    "march": 3,
    "mär": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "mai": 5,
    "may": 5,
    "juni": 6,
    "june": 6,
    "jun": 6,
    "juli": 7,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sept": 9,
    "sep": 9,
    "oktober": 10,
    "october": 10,
    "okt": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "dezember": 12,
    "december": 12,
    "dez": 12,
    "dec": 12,
}
_MONTH = r"(?P<month>[A-Za-zÄÖÜäöü]{3,9})\.?"
_YEAR = r"(?P<year>1[0-9]{3}|20[0-9]{2})"
_DAY = r"(?P<day>[0-3]?[0-9])"
_ORDINAL = r"(?:st|nd|rd|th)?"
#: (pattern, precision, confidence) — day-precise forms first, so they win over month forms.
_DATELINES: list[tuple[re.Pattern[str], str, float]] = [
    (re.compile(r"\b(?P<year>\d{4})-(?P<month_num>\d{2})-(?P<day>\d{2})\b"), "day", 0.95),
    (re.compile(rf"\b{_DAY}\.\s*{_MONTH}\s+{_YEAR}\b"), "day", 0.9),  # 12. Mai 1943
    (re.compile(rf"\b{_DAY}{_ORDINAL}\s+(?:of\s+)?{_MONTH}\s+{_YEAR}\b"), "day", 0.9),
    (re.compile(rf"\b{_MONTH}\s+{_DAY}{_ORDINAL},?\s+{_YEAR}\b"), "day", 0.9),  # May 12, 1943
    (re.compile(rf"\b{_DAY}[./](?P<month_num>[01]?[0-9])[./]{_YEAR}\b"), "day", 0.85),
    (re.compile(r"\b(?P<year>\d{4})-(?P<month_num>\d{2})\b(?!-\d)"), "month", 0.9),
    (re.compile(rf"\b{_MONTH}\s+{_YEAR}\b"), "month", 0.8),  # Mai 1943
]
_YEAR_ONLY = re.compile(rf"^\s*(?:(?:im|in|anno)\s+)?{_YEAR}\s*$", re.IGNORECASE)
#: A dateline sits at the *start* of a block: within this many characters of the first line,
#: which is room for a place ("Musterstadt, den 12. Mai 1943") and nothing much else. A date
#: further in is part of a sentence — "Stadelmann Thomas geb. am 06.04.1994" is a birth date,
#: not the day the letter was written.
DATELINE_LEAD = 20
#: …and stands nearly alone on it. "Berlin, den 3. März 1944" is a dateline; "eine Abklärung
#: wurde am 20. Mai 1943 veranlasst" is a sentence that mentions a date, and mentions are not
#: what this stores (§1: the information-origin date, not the dates the text talks about).
DATELINE_CONTEXT = 30
DATELINE_WINDOW = 160
_HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")


@dataclass(frozen=True)
class Dateline:
    date: UncertainDate
    text: str
    precision: str
    conf: float


def find_dateline(text: str, *, heading: bool = False) -> Dateline | None:
    """The date a block is *headed* by, if any — never a date mentioned further in.

    Looks at the first line only, and takes a date there for a dateline when it stands nearly
    alone on it: near the start of the line (anywhere in a heading) with little else beside
    it. A bare year counts only when the block is nothing but the year. German and English
    month names, ISO and numeric forms; day precision preferred.
    """
    first_line = text.split("\n", 1)[0].strip()
    window = first_line[:DATELINE_WINDOW]
    for pattern, precision, conf in _DATELINES:
        for match in pattern.finditer(window):
            leading = match.start() <= DATELINE_LEAD or heading
            alone = len(first_line) - (match.end() - match.start()) <= DATELINE_CONTEXT
            if not (leading and alone):
                continue
            parsed = _resolve(match, precision)
            if parsed is not None:
                return Dateline(parsed, match.group(0), precision, conf)
    whole = _YEAR_ONLY.match(text)
    if whole is not None:
        year = int(whole.group("year"))
        return Dateline(_year(year), whole.group("year"), "year", 0.7)
    return None


def _resolve(match: re.Match[str], precision: str) -> UncertainDate | None:
    groups = match.groupdict()
    year = int(groups["year"])
    if groups.get("month_num"):
        month = int(groups["month_num"])
    else:
        month = _MONTHS.get((groups.get("month") or "").lower().rstrip("."), 0)
    if not 1 <= month <= 12:
        return None
    if precision == "month":
        first = date(year, month, 1)
        last = date(year, month, calendar.monthrange(year, month)[1])
        return UncertainDate.from_bounds(first, last)
    try:
        day = date(year, month, int(groups["day"]))
    except ValueError:
        return None
    return UncertainDate.from_bounds(day, day)


def _year(year: int) -> UncertainDate:
    return UncertainDate.from_bounds(date(year, 1, 1), date(year, 12, 31))


# --- The stage ------------------------------------------------------------------------------------


class DatableNode(Protocol):
    """What the stage needs of a planned node (`snapshot._Planned`); `date` is what it sets."""

    date: DateEstimate | None

    @property
    def nid(self) -> int: ...

    @property
    def tag(self) -> str: ...

    @property
    def text_start(self) -> int: ...

    @property
    def text_end(self) -> int: ...

    @property
    def pages(self) -> set[int]: ...

    @property
    def parent(self) -> DatableNode | None: ...

    @property
    def children(self) -> Sequence[DatableNode]: ...


class DatablePage(Protocol):
    number: int
    date: DateEstimate | None


@dataclass
class DatingReport:
    content: DateEstimate | None
    stats: dict[str, object]


def date_snapshot(
    nodes: Sequence[DatableNode],
    pages: Sequence[DatablePage],
    text: str,
    *,
    hint: str | None = None,
    metadata_date: str | None = None,
    inferred: Mapping[int, DateEstimate] | None = None,
) -> DatingReport:
    """Assign an estimate to every node and page that can carry one; return the content's.

    1. Datelines in leaf blocks and headings ⇒ `EXPLICIT` node estimates (the anchors).
    1b. `inferred` — what a model made of the nodes it could place (`INFERRED`) — fills the
       nodes no dateline governs. A printed dateline always wins: a model reads the document,
       the document states itself.
    2. A page with anchors ⇒ the `AGGREGATED` envelope of them.
    3. Undated pages between anchor pages ⇒ `INTERPOLATED` [previous, next] — only while the
       anchors are in non-decreasing order (reading order ≈ chronological order: diaries,
       logbooks; not scrapbooks). Before the first / after the last anchor ⇒ one-sided.
    4. Containers with dated descendants ⇒ `AGGREGATED`, bottom-up.
    5. The content ⇒ the envelope over dated pages and top-level nodes; failing that, the
       user's `date_hint` (`INFERRED`) or the file's metadata date (`METADATA`) as a prior.
    6. What is still undated inherits: a node from its pages (narrowed to its dated ancestor),
       else from the nearest dated ancestor, else — like an undated page — from a content
       date that is a prior. Gaps under an aggregated content stay NULL.

    A prior also narrows the open sides interpolation leaves at the ends of a document.
    """
    stats: dict[str, object] = {}
    anchors = []
    for node in nodes:
        if node.children:
            continue
        found = find_dateline(text[node.text_start : node.text_end], heading=node.tag in _HEADINGS)
        if found is not None:
            node.date = DateEstimate(found.date, DateSource.EXPLICIT, found.conf)
            anchors.append(
                {
                    "nid": node.nid,
                    "pages": sorted(node.pages),
                    "text": found.text,
                    "edtf": found.date.edtf,
                }
            )
    stats["anchors"] = anchors

    placed = 0
    for node in nodes:
        estimate = (inferred or {}).get(node.nid)
        if node.date is None and estimate is not None:
            node.date = estimate
            placed += 1
    if inferred:
        stats["inferred"] = {"offered": len(inferred), "used": placed}

    explicit_on: dict[int, list[DateEstimate]] = defaultdict(list)
    for node in nodes:
        if node.date is not None and node.date.source == DateSource.EXPLICIT:
            for number in node.pages:
                explicit_on[number].append(node.date)
    ordered = sorted(pages, key=lambda page: page.number)
    for page in ordered:
        if explicit_on[page.number]:
            page.date = aggregate(explicit_on[page.number])

    prior = _prior(hint, metadata_date, stats)
    stats["interpolation"] = _interpolate(ordered, prior)

    for node in reversed(nodes):  # children come after their parent in pre-order
        if node.children:
            dated = [child.date for child in node.children if child.date is not None]
            if dated:
                node.date = aggregate(dated)

    top = [node.date for node in nodes if node.parent is None and node.date is not None]
    on_pages = [page.date for page in ordered if page.date is not None]
    content = aggregate(top + on_pages) if top or on_pages else None
    if content is None:
        content = prior

    fill = content is not None and content.source in (DateSource.INFERRED, DateSource.METADATA)
    for page in ordered:
        if page.date is None and fill and content is not None:
            page.date = inherit(content)
    by_number = {page.number: page for page in ordered}
    for node in nodes:
        if node.date is not None:
            continue
        ancestor = _dated_ancestor(node)
        from_pages = [by_number[n].date for n in sorted(node.pages) if by_number[n].date]
        union = aggregate([d for d in from_pages if d is not None]) if from_pages else None
        if union is not None:
            if ancestor is not None and ancestor.date is not None:
                narrowed = union.date.intersect(ancestor.date.date) or ancestor.date.date
                node.date = inherit(union, narrowed_to=narrowed)
            else:
                node.date = inherit(union)
        elif ancestor is not None and ancestor.date is not None:
            node.date = inherit(ancestor.date)
        elif fill and content is not None:
            node.date = inherit(content)

    stats["dated_nodes"] = sum(1 for node in nodes if node.date is not None)
    stats["dated_pages"] = sum(1 for page in ordered if page.date is not None)
    return DatingReport(content=content, stats=stats)


def _prior(
    hint: str | None, metadata_date: str | None, stats: dict[str, object]
) -> DateEstimate | None:
    if hint:
        try:
            stats["hint"] = hint
            return DateEstimate(UncertainDate.parse(hint), DateSource.INFERRED, 0.5)
        except InvalidDate as exc:
            stats["hint"] = f"ignored: {exc}"
    if metadata_date:
        try:
            return DateEstimate(UncertainDate.parse(metadata_date), DateSource.METADATA, 0.3)
        except InvalidDate:
            stats["metadata_date"] = f"ignored: {metadata_date!r}"
    return None


def _monotonic(dates: Sequence[UncertainDate]) -> bool:
    for before, after in zip(dates, dates[1:], strict=False):
        if before.min is not None and after.min is not None and after.min < before.min:
            return False
        if before.max is not None and after.max is not None and after.max < before.max:
            return False
    return True


def _interpolate(ordered: Sequence[DatablePage], prior: DateEstimate | None) -> str:
    """Step 3; returns what happened, for `stats["dating"]`."""
    if not ordered:
        return "no pages"
    anchored = [page for page in ordered if page.date is not None]
    if not anchored:
        return "no anchors"
    anchor_dates = [page.date.date for page in anchored if page.date is not None]
    if not _monotonic(anchor_dates):
        pages = ", ".join(f"{p.number}: {p.date.date.edtf}" for p in anchored if p.date)
        return f"skipped: anchors are not in chronological order ({pages})"
    done = 0
    for page in ordered:
        if page.date is not None:
            continue
        # Only the anchors count as neighbours — never a page this loop just interpolated.
        previous = next((p for p in reversed(anchored) if p.number < page.number), None)
        following = next((p for p in anchored if p.number > page.number), None)
        lower = previous.date.date.max if previous and previous.date else None
        upper = following.date.date.min if following and following.date else None
        if lower is not None and upper is not None and lower > upper:  # overlapping anchors
            lower = previous.date.date.min if previous and previous.date else None
            upper = following.date.date.max if following and following.date else None
        estimate = UncertainDate.from_bounds(lower, upper)
        if prior is not None and not estimate.is_bounded:
            estimate = estimate.intersect(prior.date) or estimate
        distance = min(
            page.number - previous.number if previous else 10**6,
            following.number - page.number if following else 10**6,
        )
        two_sided = previous is not None and following is not None
        base = 0.7 if two_sided else 0.5
        floor = 0.2 if two_sided else 0.1
        conf = max(floor, round(base - 0.1 * (distance - 1), 2))
        page.date = DateEstimate(estimate, DateSource.INTERPOLATED, conf)
        done += 1
    return f"applied to {done} page(s)"


def _dated_ancestor(node: DatableNode) -> DatableNode | None:
    parent = node.parent
    while parent is not None:
        if parent.date is not None:
            return parent
        parent = parent.parent
    return None


# --- Invariants -----------------------------------------------------------------------------------


def check_dating(
    nodes: Sequence[DatableNode], pages: Sequence[DatablePage], content: DateEstimate | None
) -> list[str]:
    """What must hold after the stage — asserted by the builder, tested in CI:

    - a dated child lies within its dated parent (the node tree), and every dated node and
      page within the content's range — on the sides where both bounds are known
      (`UncertainDate.contains`);
    - an `AGGREGATED` row is the *tight* envelope of what it was built from: a container of
      its dated children, a page of the explicit nodes on it, the content of the dated pages
      and top-level nodes;
    - every EDTF string re-derives to exactly its bounds.
    """
    problems: list[str] = []
    for node in nodes:
        if node.date is None:
            continue
        problems += _round_trip(f"node #{node.nid}", node.date.date)
        parent = node.parent
        if parent is not None and parent.date is not None:
            if not parent.date.date.contains(node.date.date):
                problems.append(
                    f"node #{node.nid} {node.date.date.edtf} leaves its parent "
                    f"#{parent.nid} {parent.date.date.edtf}"
                )
        if content is not None and not content.date.contains(node.date.date):
            problems.append(
                f"node #{node.nid} {node.date.date.edtf} leaves the content {content.date.edtf}"
            )
        if node.date.source == DateSource.AGGREGATED:
            dated = [c.date.date for c in node.children if c.date is not None]
            if UncertainDate.envelope(dated) != node.date.date:
                problems.append(
                    f"node #{node.nid}: aggregated range is not its children's envelope"
                )
    explicit_on: dict[int, list[UncertainDate]] = defaultdict(list)
    for node in nodes:
        if node.date is not None and node.date.source == DateSource.EXPLICIT:
            for number in node.pages:
                explicit_on[number].append(node.date.date)
    for page in pages:
        if page.date is None:
            continue
        problems += _round_trip(f"page {page.number}", page.date.date)
        if content is not None and not content.date.contains(page.date.date):
            problems.append(
                f"page {page.number} {page.date.date.edtf} leaves the content {content.date.edtf}"
            )
        if page.date.source == DateSource.AGGREGATED:
            if UncertainDate.envelope(explicit_on[page.number]) != page.date.date:
                problems.append(
                    f"page {page.number}: aggregated range is not its datelines' envelope"
                )
    if content is not None:
        problems += _round_trip("content", content.date)
        if content.source == DateSource.AGGREGATED:
            parts = [n.date.date for n in nodes if n.parent is None and n.date is not None]
            parts += [p.date.date for p in pages if p.date is not None]
            if UncertainDate.envelope(parts) != content.date:
                problems.append("content: aggregated range is not its pages' and nodes' envelope")
    return problems


def _round_trip(what: str, value: UncertainDate) -> list[str]:
    try:
        again = UncertainDate.parse(value.edtf)
    except InvalidDate as exc:
        return [f"{what}: {exc}"]
    if (again.min, again.max) != (value.min, value.max):
        return [
            f"{what}: {value.edtf} re-derives to {again.min}..{again.max}, "
            f"stored {value.min}..{value.max}"
        ]
    return []
