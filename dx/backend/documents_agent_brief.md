# Agent Brief — `app/documents` (Django)

## 0. Mission and sources of truth

Implement the document-extraction domain model in the Django app **`app/documents`**.

- **Schema source of truth: `documents_model_v7.puml`** (attached). Class names, field names, types, constraints, enums, and FK edges are normative. The legend maps UML notation to Django: filled diamond = `on_delete=CASCADE`, solid arrow = `PROTECT`, dashed arrow = `SET_NULL, null=True`. The `api` sections inside classes are methods/properties to implement.
- **This document is everything the diagram cannot express**: write-path contract, conventions, algorithms, ops jobs, non-goals, open decisions, and acceptance tests. Where this brief and the diagram overlap, the diagram wins on *structure*, this brief wins on *behavior*.

Architecture in one paragraph, for orientation: a `Document` is a mutable aggregate root that facades an **immutable snapshot** (`DocumentContent` + `Page` + `Node` + `PageRegion`). Each extraction produces a whole new snapshot; nothing inside a completed snapshot is ever updated. `content.html` is THE artifact (one sanitized semantic HTML document); `Node` rows are a SQL index into it; `PageRegion` rows are the physical geometry linking nodes to pages; confidence is stored once per word and aggregated upward via additive stats. Because snapshots are immutable, every piece of denormalization (offsets, materialized paths, page lists in HTML attributes, conf rollups) is a safe precomputed index, not a second source of truth.

## 1. Stack and ground rules

- Django 5.x, PostgreSQL (JSONB available; assume ≥14), psycopg. All `JSONField`s are JSONB.
- **No PostGIS, no django-treebeard/MPTT, no Word table.** These were considered and rejected; do not introduce them.
- HTML sanitization with **`nh3`** (not `bleach` — deprecated).
- UUID primary keys where the diagram says so; `BigAutoField` for `Extractor`.
- The **extraction pipeline is the only writer** of snapshot rows. Django admin for `DocumentContent`, `Page`, `Node`, `PageRegion`, `Blob` must be read-only. Provide no code paths that update completed snapshots.
- `related_name`s (so facade code reads like the diagram): `Document.contents`, `DocumentContent.pages`, `DocumentContent.nodes`, `Node.children`, `Node.regions`, `Page.regions`. All FKs *to* `Blob` use `related_name="+"`; the GC job queries referencing columns explicitly.
- `DocumentContent.successful()` is a **queryset/manager method**, never a filtered *default* manager (default-manager filtering breaks related managers, admin, and cascades).

## 2. Schema details beyond the diagram

- Partial unique: `UniqueConstraint(fields=["document"], condition=Q(is_current=True), name="one_current_content_per_document")`.
- Add `CheckConstraint`s: `html_start <= html_end`, `text_start <= text_end` (Node); `0 <= x0 <= x1 <= 1` and `0 <= y0 <= y1 <= 1` (PageRegion); `number >= 1` (Page).
- `Node.path`: materialized path, zero-padded 4-digit decimal segments joined by `.` (e.g. `0002.0011.0001`), assigned from sibling order. Index `(content, path)` using `varchar_pattern_ops` so `path LIKE 'prefix%'` uses the index. 255 chars ≈ 50 levels — plenty.
- `Node.nid`: pre-order document-order numbering starting at 1; equals the `data-nid` attribute in the HTML.
- FTS: GIN functional index on `to_tsvector('simple', text)` on `DocumentContent`. Known limitation: Postgres tsvectors cap at 1MB / 16383 positions — acceptable for v1; note it and revisit for very large books (fallbacks: node-level chunked search or pg_trgm).
- `Blob.file`: content-addressed storage path sharded by hash, e.g. `blobs/ab/cd/<sha256>`. Compute sha256 while streaming the upload; dedup with `get_or_create(sha256=...)`; never overwrite an existing file.
- `conf_stats` JSON schema (same at every level): `{"n": int, "sum": float, "min": float, "max": float, "hist": [int × 10]}` — 10 uniform buckets over [0,1], last bucket right-inclusive. Merging = elementwise addition (plus min/max). `NULL` means "no OCR happened here" (born-digital); never write a fake 1.0.
- `PageRegion.words` entry format: `[x0, y0, x1, y1, text_start, text_end, conf]` (conf may be null per word).
- `PageRegion.detect_conf` is the layout model's confidence in the region itself — never mix it into text-quality rollups.

## 3. Conventions (get these wrong and everything downstream is subtly broken)

- **Offsets are Python codepoint offsets** into `content.text` / `content.html`, end-exclusive (`[start, end)`), globally addressed (one coordinate space per snapshot; region offsets lie within their node's range; ancestor ranges enclose descendants). JavaScript strings count UTF-16 units — if a frontend does highlighting, convert at the API boundary, not in the client. Include an emoji/astral-char test.
- **Coordinates normalized to [0,1], origin top-left, y down** (image convention). PDF's native origin is bottom-left: **flip once, at extraction**, nowhere else.
- **Polygons are implicitly closed rings** — list of `[x, y]` points, last connects to first, do not repeat the first point. Envelope (`x0..y1`) is derived min/max at write time. `polygon IS NULL` ⇒ the region is its envelope.
- **HTML pipeline ordering matters**: build tree → serialize to the final string → sanitize with nh3 → *then* measure `html_start`/`html_end` on the exact stored string. Never measure before the last mutation.
- HTML attribute conventions: every structural tag carries `data-nid="<int>"` and `data-pages="12,13"` (ascending, comma-separated). Tag allowlist (v1, also the sanitizer allowlist — this *is* the type vocabulary): `section, h1–h6, p, ul, ol, li, table, thead, tbody, tr, th, td, figure, figcaption, blockquote, pre, code`. Attribute allowlist: `data-nid`, `data-pages` everywhere; `colspan`, `rowspan` on `td`/`th`.

## 4. The write path (snapshot builder contract)

1. `Document.reextract(extractor)` creates `DocumentContent(document, blob=document.source_blob, extractor, status=PENDING)`, dispatches an async task, returns the row. (Optional nicety: skip if an identical PENDING/RUNNING (document, blob, extractor) exists.)
2. Task sets `RUNNING` + `started_at`, runs the extractor, stores the extractor-native payload as a Blob → `raw_output`.
3. **One transaction** writes the entire snapshot: html + text (plain projection, `"\n\n"`-joined) + `bulk_create` of Pages, Nodes, Regions; conf_stats computed strictly bottom-up (words → region → node-subtree → page → content); then terminal status (`SUCCEEDED`/`PARTIAL` — PARTIAL means some pages failed, record which in `stats`).
4. After a terminal status, the row and its children are frozen. Failure ⇒ `FAILED` + `error`; no child rows required.
5. **The current flip** is its own tiny transaction and the only place `is_current` or `current_content` ever change:

```python
with transaction.atomic():
    doc.contents.filter(is_current=True).update(is_current=False)
    new.is_current = True
    new.save(update_fields=["is_current"])
    doc.current_content = new
    doc.save(update_fields=["current_content"])
```

`is_current` (+ partial unique index) is the DB-enforced truth; the pointer is the read accelerator. The old snapshot serves reads until commit. Emit a `content_switched` Django signal here — downstream consumers (search index, embeddings) must store the `DocumentContent` id they were built from and re-build on this signal (**invalidate-and-recompute** is the chosen policy; no cross-run alignment).

6. Extractor selection: a simple mime→extractor mapping in the service layer. No Document subclasses per format, ever. HTML-source documents produce snapshots with **zero pages** — everything must tolerate that (likewise zero nodes for flat scans).
7. Post-build assertions (cheap, run in the builder, also as tests): `content.conf_stats == Σ regions.conf_stats`; every region's `[text_start, text_end)` ⊆ its node's range; every child node's ranges ⊆ parent's; `content.html[n.html_start:n.html_end]` starts with `<` + n.tag.

## 5. Read-path algorithms

- `Page.hit(x, y)`: SQL envelope filter (`x0<=x<=x1 AND y0<=y<=y1` over `regions` of the page) → point-in-polygon refine in Python where `polygon` is set → smallest area wins → return its node. Word-level lookup = same, then scan the winning region's `words` for the containing box.
- `Page.reduced_html()`: distinct nodes having regions on the page; drop any node whose ancestor is also in the set (path-prefix check); concatenate their `html()` slices in document order. A paragraph straddling pages appears **in full on both** — intended.
- FTS hit offset → node: `nodes.filter(text_start__lte=o, text_end__gt=o).order_by(F("text_end")-F("text_start")).first()` (deepest/smallest wins; uses index `(content, text_start)`).
- `Node.html()` / `Node.text()`: pure string slices of the immutable artifacts — no parser at read time. `subtree()` = `path LIKE` prefix query. `regions()`/`boxes` scale to pixels by multiplying normalized coords by the render size (that's why `Page.width/height` exist for source-resolution crops).
- Facade: all `Document` properties delegate to `current_content` and must be **empty-safe** (fresh document, extraction still running ⇒ `""` / `.none()` / `None`, no exceptions). DRF serializers go through the facade; snapshot rows never appear in the public API shape.
- `diff(other)`: v1-minimal — status/stats deltas, node-set comparison keyed by `(path, tag, title)`, plain text similarity ratio. Low priority.

## 6. Ops (schema needs nothing; code must exist)

- `prune_contents` management command: delete non-current, terminal snapshots older than N days (keep the current one and, optionally, the latest successful per extractor). Never touch `is_current=True`.
- `gc_blobs` management command: delete `Blob` rows (and files) referenced by **no** FK — check all five referencing columns: `Document.source_blob`, `Document.thumbnail`, `DocumentContent.blob`, `DocumentContent.raw_output`, `Page.thumbnail`.
- History grows with every re-extraction by design; these two jobs are the counterweight.

## 7. Explicit non-goals (decided against — do not build)

- No curation/edit workflow (the `CURATED` enum value exists for the future; nothing writes it in v1).
- No document versioning via re-uploaded source blobs (schema already tolerates it — each snapshot pins its blob — but no UX/flow).
- No `ExtractionJob`/artifact split — process fields deliberately live on `DocumentContent` (Wagtail-style fold).
- No storage of non-semantic page furniture (running headers, footers, page numbers): clicking them returns nothing, and that is correct.
- No `TABLE_CELL` nodes yet — clicking a table selects the whole table.
- No Word rows, no MPTT, no PostGIS, no Markdown content format, no per-page status table (PARTIAL + `stats` covers it).
- Multi-file documents (HTML + assets, TIFF folders, email + attachment) are out of scope: one source blob per document.
- `words` / `word_boxes` population may be deferred (schema-ready); everything else must degrade gracefully when they're absent.

## 8. Open decisions — proposed defaults (proceed with these unless the human overrides)

1. **Images/figures in the HTML**: v1 emits `<figure>`/`<figcaption>` *without* `<img>`; figure pixels are obtained on demand by cropping the page render via the region geometry. (Alternative later: crop-to-Blob and `src` it.)
2. **FTS language config**: `'simple'` (documents are multilingual; revisit per-language later).
3. **`Document.title`**: caller-provided at ingest; fallback = first `h1`/highest heading of the current snapshot.
4. **Node.title**: for `section` nodes, the text of their heading child; for headings, their own text; else null.
5. Histogram buckets: 10 uniform as specified.

## 9. Acceptance tests (minimum set)

1. Conf rollup invariant: content == Σ regions == Σ over words, at every level; merging two children's stats equals recomputing.
2. Offsets survive astral characters (emoji in text before a node shifts nothing).
3. `node.html()` equals lxml's outerHTML of the `data-nid` element, for a sample of nodes.
4. Second `is_current=True` for the same document raises `IntegrityError`; concurrent flips leave exactly one current.
5. Flip is atomic: readers mid-transaction see the old snapshot; `content_switched` fires once.
6. `hit()` on a rotated polygon: point inside polygon A's shape but only inside B's *envelope* resolves to A; overlap ties go to the smallest area; word lookup returns the right conf.
7. `reduced_html()` deduplicates ancestors and includes straddling nodes on both pages.
8. Zero-page (HTML source) and zero-node snapshots: facade, `reduced_html`, confidence, outline all behave (empty, not crashing).
9. Blob dedup: same bytes twice ⇒ one row, one file; GC removes an orphaned blob and spares every referenced one.
10. PDF y-flip: a known region drawn over the rendered page visually aligns (fixture with expected pixel box).
11. Cross-page paragraph ⇒ two regions; `regions()` returns both; `data-pages` lists both pages.
12. FTS hit offset maps to the deepest containing node.
13. Sanitizer: disallowed tag/attr in extractor output is stripped *before* offset measurement (offsets still verify).
14. Prune never deletes the current snapshot; facade survives `current_content=NULL` (SET_NULL path).

## 10. Suggested build order

(1) models + constraints + migrations + read-only admin → (2) Blob service (hashing, dedup, storage, GC) → (3) snapshot builder with a **fake extractor fixture** + the §4.7 assertions → (4) facade + read-path algorithms + tests §9 → (5) flip/signal/prune commands → (6) real adapter (docling maps nearly 1:1: items→Nodes, prov→PageRegions, export→html).
