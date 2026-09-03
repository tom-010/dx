
# Patient Timeline – Implementation Guide

This guide is written for a coding agent. It contains the decisions that are already made, the interfaces to build, and the acceptance criteria. Where it says "the project's existing X", look it up in the codebase instead of inventing a new one.

## 0. Decisions already made (do not re-open)

- **Materialized** timeline: one `TimelineEvent` table, written in the same DB transaction as the domain change. Modules stay source of truth; the table is a rebuildable projection.
- **Common fields on the event** (`title`, `description`, `image_url`, `event_type`) so most cards render generically. A `payload` JSON column carries type-specific extras.
- **Backend knows nothing about SPA routes.** Click handling lives in a frontend registry keyed by `event_type`. The backend only ships a `source` reference (`{type, id}`).
- **Registry pattern on both sides.** Backend: modules declare an `EventType` and register it. Frontend: feature modules register a handler per `event_type`. Dependency direction is always `module → timeline`, never reverse.
- **Explicit service calls**, no `post_save` signals. Only deletion uses a signal (generic `post_delete` in the timeline app).
- Phase 1 = technical events (`documents.uploaded`, `speech.recorded`). Phase 2 (real-world events from extraction) must fit without schema changes; see §6.
- Stack: Django + PostgreSQL, Django Ninja, pydantic; React SPA with shadcn/ui and a generated OpenAPI SDK.

## 2. Backend

### 2.1 Model (`models.py`)

``` python
class EventKind(models.TextChoices):
    TECHNICAL = "technical"
    REAL_WORLD = "real_world"

class DatePrecision(models.TextChoices):
    DATETIME = "datetime"; DAY = "day"; MONTH = "month"; YEAR = "year"

class EventStatus(models.TextChoices):
    ACTIVE = "active"; SUGGESTED = "suggested"; REJECTED = "rejected"

class TimelineEvent(models.Model):
    id             = UUIDField(primary_key=True, default=uuid4, editable=False)
    patient        = ForeignKey(<project Patient model>, on_delete=CASCADE, related_name="timeline_events")
    event_type     = CharField(max_length=100, db_index=True)     # "documents.uploaded"
    kind           = CharField(max_length=20, choices=EventKind.choices)
    status         = CharField(max_length=20, choices=EventStatus.choices, default=EventStatus.ACTIVE)

    occurred_at    = DateTimeField()                                # sort key
    occurred_until = DateTimeField(null=True, blank=True)
    date_precision = CharField(max_length=10, choices=DatePrecision.choices, default=DatePrecision.DAY)
    recorded_at    = DateTimeField(auto_now_add=True)
    updated_at     = DateTimeField(auto_now=True)

    title          = CharField(max_length=200)
    description    = TextField(blank=True, default="")
    image_url      = CharField(max_length=500, blank=True, default="")
    payload        = JSONField(default=dict, blank=True)           # jsonb

    actor          = ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=SET_NULL)

    content_type   = ForeignKey(ContentType, on_delete=CASCADE)
    object_id      = CharField(max_length=64)                       # str: works for int and UUID PKs
    source         = GenericForeignKey("content_type", "object_id")

    class Meta:
        constraints = [
            UniqueConstraint(fields=["content_type", "object_id", "event_type"], name="timeline_uniq_source_type"),
            CheckConstraint(check=Q(occurred_until__isnull=True) | Q(occurred_until__gte=F("occurred_at")), name="timeline_range_valid"),
        ]
        indexes = [
            Index(fields=["patient", "-occurred_at", "-id"], name="timeline_feed_idx"),
            Index(fields=["content_type", "object_id"], name="timeline_source_idx"),
        ]
```

Rules:

- `occurred_at` is timezone-aware UTC. For `DAY`/`MONTH`/`YEAR` precision, store **12:00 UTC** of the first day of the period so the date never shifts when converting to a local timezone. Put this normalization into `services.record`, not into the callers.
- `image_url` is whatever URL the SPA can already load (typically an existing API/media path). The timeline does not store or proxy images.
- `payload` is for type-specific extras only; if something belongs on every card, it is a column.
- Optional on source models: `timeline_events = GenericRelation("timeline.TimelineEvent")` for `doc.timeline_events.all()`. Not required.

### 2.2 Public contracts (`contracts.py`)

This is the interface other modules program against. Keep it small, typed, and stable.

``` python
class EventData(BaseModel):
    """Everything the timeline needs to know about one event. Returned by EventType.describe()."""
    patient_id: UUID | int
    occurred_at: datetime
    occurred_until: datetime | None = None
    date_precision: DatePrecision = DatePrecision.DAY
    title: str = Field(max_length=200)
    description: str = ""
    image_url: str = ""
    payload: BaseModel | dict = {}
    actor_id: int | None = None

class EventType(ABC):
    key: ClassVar[str]                       # "<app_label>.<snake_name>", unique
    kind: ClassVar[EventKind]
    model: ClassVar[str]                     # "documents.Document" (label string, not the class → no import cycles)
    label: ClassVar[str]                     # shown in filter UI
    description: ClassVar[str] = ""
    payload_schema: ClassVar[type[BaseModel] | None] = None   # validated on write; also exported to OpenAPI (§2.5)

    @abstractmethod
    def describe(self, obj) -> EventData: ...

    def backfill(self) -> QuerySet:          # all objects that should currently have this event
        return apps.get_model(self.model)._default_manager.all()

class EventTypeRegistry:
    def register(self, cls: type[EventType]) -> type[EventType]   # decorator; raises on duplicate key or bad key format
    def get(self, key: str) -> EventType
    def for_model(self, model: type[Model]) -> list[EventType]
    def all(self) -> list[EventType]

registry = EventTypeRegistry()
```

Design notes:

- One `describe()` instead of five getters keeps module code short and lets the timeline validate the whole thing at once (pydantic).
- `payload_schema` gives you three things: validation on write, documentation, and typed payloads in the SDK.
- Modules register in `AppConfig.ready()`: `from . import timeline_events # noqa`. Convention: every module that emits events has a `timeline_events.py`.

### 2.3 Services (`services.py`)

``` python
def record(key: str, source: Model, *, actor=None) -> TimelineEvent
    # describe() → validate → normalize date → update_or_create on (content_type, object_id, event_type).
    # Runs in the caller's transaction. No side effects beyond the row.

def record_many(key: str, sources: Iterable[Model]) -> int
    # bulk_create(update_conflicts=True, unique_fields=[...], update_fields=[...]) → one INSERT ... ON CONFLICT.

def remove(source: Model, key: str | None = None) -> int
    # Hard-delete events for a source (all types, or one). Modules call this on soft-delete/unpublish.

def rebuild(*, key: str | None = None, patient_id=None, chunk_size=1000) -> int
    # For each registered type (or one): iterate backfill() in chunks, record_many, then delete stale
    # events of that type whose source is no longer in backfill(). Idempotent.

def events_for(source: Model) -> QuerySet[TimelineEvent]
```

All functions raise `UnknownEventType` for unregistered keys. `record` must be safe to call twice (second call updates title/description/etc. from the fresh `describe()`).

### 2.4 Deletion + system checks

- `signals.py`: connect `post_delete` **without** a sender. In the handler, `if registry.for_model(type(instance)): TimelineEvent.objects.filter(content_type=..., object_id=str(instance.pk)).delete()`. Cheap because `for_model` is a dict lookup. Required for erasure requests.
- `checks.py` (register via `@register(Tags.models)`): every key matches `^[a-z0-9_]+\.[a-z0-9_]+$`, prefix equals the app label of `model`, `model` resolves, `payload_schema` is a pydantic model or `None`.

### 2.5 API (Ninja, `api.py` / `schemas.py`)

Mount a `Router` under the project's existing patient-scoped prefix and reuse its patient authorization dependency. Give every operation an explicit `operation_id` – these become the SDK function names.

| Method | Path                                                | operation_id                | Purpose                                       |
|--------|-----------------------------------------------------|-----------------------------|-----------------------------------------------|
| GET    | `/patients/{patient_id}/timeline/events`            | `timeline_list_events`      | cursor-paginated feed                         |
| GET    | `/patients/{patient_id}/timeline/events/{event_id}` | `timeline_get_event`        | single event (deep links, detail sheet)       |
| GET    | `/patients/{patient_id}/timeline/histogram`         | `timeline_histogram`        | counts per month/year for the navigation rail |
| GET    | `/timeline/event-types`                             | `timeline_list_event_types` | registry dump for the filter UI               |

Query params for the list: `cursor`, `limit` (default 30, max 100), `kind`, `types` (comma list), `from`, `to`, `status` (default `active`; phase 2 UI passes `active,suggested`).

Schemas:

``` python
class SourceRef(Schema):
    type: str
    id: str  # "documents.document", "123"


class ActorOut(Schema):
    id: int
    display_name: str


class TimelineEventOut(Schema):
    id: UUID
    event_type: str
    kind: EventKind
    status: EventStatus
    occurred_at: datetime
    occurred_until: datetime | None
    date_precision: DatePrecision
    recorded_at: datetime
    title: str
    description: str
    image_url: str
    actor: ActorOut | None
    source: SourceRef
    payload: dict


class TimelineEventPage(Schema):
    items: list[TimelineEventOut]
    next_cursor: str | None


class EventTypeOut(Schema):
    key: str
    kind: EventKind
    label: str
    description: str


class HistogramBucket(Schema):
    period: date
    total: int
    technical: int
    real_world: int
```

Implementation notes:

- **Cursor** = base64url of `{"t": occurred_at_iso, "id": uuid}`. Filter: `Q(occurred_at__lt=t) | Q(occurred_at=t, id__lt=id)`, order `-occurred_at, -id`. Fetch `limit + 1` to know whether there is a next page.
- **Histogram**: `annotate(period=TruncMonth("occurred_at"))` (or `TruncYear`), `values("period").annotate(total=Count("id"), technical=Count("id", filter=Q(kind=...)), ...)`. Respect the same filters as the list except `cursor`.
- `select_related("actor")` on the list. Never resolve `source` objects in the feed; the common fields exist so you don't have to.
- **Typed payloads in OpenAPI (recommended, small effort):** in the place where the router is mounted (after app loading, e.g. in the API module imported from `urls.py`), build `PayloadUnion = Union[tuple(t.payload_schema for t in registry.all() if t.payload_schema)]` and use `payload: PayloadUnion | dict` on `TimelineEventOut`. The SDK then knows the payload shape per type. If this causes import-order trouble, fall back to `dict` and move on; it is not on the critical path.

### 2.6 Management command

`manage.py rebuild_timeline [--type KEY] [--patient ID] [--dry-run]` → calls `services.rebuild`, prints counts per type. This is also the migration path for pre-existing data: run it once after deploy.

### 2.7 Testing helpers (`testing.py`)

``` python
def assert_event(source, key, **expected)      # fetches the event, asserts fields, returns it
def assert_no_event(source, key=None)
```

Other modules' tests should use these rather than querying `TimelineEvent` directly.

### 2.8 First integration (reference implementation)

`apps/documents/timeline_events.py`:

``` python
class DocumentUploadedPayload(BaseModel):
    mime_type: str
    page_count: int | None = None


@registry.register
class DocumentUploaded(EventType):
    key = "documents.uploaded"
    kind = EventKind.TECHNICAL
    model = "documents.Document"
    label = "Document uploaded"
    payload_schema = DocumentUploadedPayload

    def describe(self, doc):
        return EventData(
            patient_id=doc.patient_id,
            occurred_at=doc.created_at,
            date_precision=DatePrecision.DATETIME,
            title=doc.title or doc.original_filename,
            description=f"{doc.page_count} pages" if doc.page_count else "",
            image_url=doc.thumbnail_url or "",
            payload=DocumentUploadedPayload(mime_type=doc.mime_type, page_count=doc.page_count),
            actor_id=doc.uploaded_by_id,
        )

    def backfill(self):
        return Document.objects.filter(deleted_at__isnull=True)
```

In the document upload service, inside the existing `transaction.atomic`: `timeline.record("documents.uploaded", doc, actor=user)`. On soft delete: `timeline.remove(doc)`. Same pattern for `speech.recorded`.

Adapt field names to the real `Document` model; do not guess.

## 3. Frontend

### 3.1 UI concept

Think of it as a **medical diary with a quiet technical log woven in**, not as a social feed.

**Layout**

- Main column: a vertical spine with cards hanging off one side (alternating sides looks like old Facebook but scans badly and breaks on mobile; don't). Newest first.
- Right rail (desktop, sticky): a **date navigator** listing years, expandable to months, each with a small density indicator built from the histogram endpoint. Clicking a period restarts the feed with `to=<end of period>`. This is the one feature that makes a long medical history usable; treat it as core, not decoration.
- Top: a compact **filter bar** – a kind toggle (All / Real world / Technical) and a type multi-select populated from the event-types endpoint. Filter state lives in the URL so views are shareable and survive reloads.
- Mobile: rail collapses into a "Jump to…" sheet; filters into a drawer.

**Two visual weights** – this is the most important design decision:

- *Real-world events* are full cards: icon, title, description (2–3 lines, clamped), optional image, meta line. They are what the patient actually cares about.
- *Technical events* are **quiet rows**: single line, muted, small icon, "You uploaded *Arztbrief Dr. Müller*", timestamp. They must never visually compete with real-world events.
- **Roll-ups:** consecutive technical events of the same type within the same day collapse into one row ("You uploaded 4 documents") that expands inline. Do this client-side after grouping. Without roll-ups the technical log will drown everything else the moment extraction goes live.

**Time rendering** is precision-aware and grouped:

- Sticky group headers per day (`DATETIME`/`DAY`), per month (`MONTH`), per year (`YEAR`). A year-only event does not pretend to be 1 January.
- Ranges render as "Mar–Apr 2019" or "12.–18.03.2019".
- Within a group, order by `occurred_at` desc; show clock time only for `DATETIME` precision.
- Provide a small "relative" hint on technical events ("2 hours ago") but always keep the absolute date accessible via tooltip.

**Card anatomy** (generic card, used unless a type registers its own):

- Left: type icon in a subtle colored circle (icon and color come from the frontend registry).
- Title (strong), description (muted, clamped), optional image as a small thumbnail on the right, not a hero image.
- Meta line: precise date · actor ("you" / display name) · source chip ("from Arztbrief …", phase 2).
- Whole card is the click target when the registry provides `onOpen`; otherwise it is not clickable and has no hover affordance. Don't fake interactivity.
- Overflow menu placeholder for phase 2 (confirm / reject / hide).
- Phase 2 `suggested` status: dashed border, "Suggested" badge, inline Confirm/Dismiss actions.

**States**

- Skeleton cards matching the two weights while loading; infinite scroll via intersection observer with a "Load more" button fallback.
- Empty state per situation: no events at all ("Upload a document or record a note to get started") vs. filters hide everything ("No events match — clear filters").
- Error state with retry that keeps already loaded pages.
- Deep link: `?event=<id>` opens the event's detail sheet using `timeline_get_event` even before the feed is loaded.

**shadcn building blocks** (use what fits, don't force it): `Card`, `Badge`, `Avatar`, `Tooltip`, `Skeleton`, `ToggleGroup` (kind filter), `Popover`/`Command` (type filter), `Collapsible` (roll-ups), `ScrollArea` (rail), `Sheet` (mobile navigator + detail), `DropdownMenu` (overflow).

**Accessibility:** the feed is a `<ul>`/`<li>` list with group headings; cards with `onOpen` are buttons/links, not divs with click handlers; the rail is keyboard-navigable.

### 3.2 Frontend architecture

Mirror the backend: one registry, feature modules register themselves.

``` ts
// features/timeline/registry.ts
export interface TimelineEventTypeHandler {
  key: string;                                   // "documents.uploaded"
  icon: LucideIcon;
  tone?: "neutral" | "info" | "success" | ...;   // maps to card accent colors
  Card?: ComponentType<{ event: TimelineEvent }>; // optional override of the generic card
  onOpen?: (event: TimelineEvent, ctx: OpenContext) => void;  // ctx: navigate, openSheet, …
  rollupLabel?: (events: TimelineEvent[]) => string;          // "Uploaded 3 documents"
}
export function registerTimelineEventType(h: TimelineEventTypeHandler): void
export function getHandler(key: string): TimelineEventTypeHandler | undefined
```

- `features/documents/timeline.ts` registers `documents.uploaded` with `onOpen: (e, {navigate}) => navigate(routes.document(e.source.id))`.
- A single `features/timeline/registrations.ts` imports all feature registration files and is imported once at app bootstrap. Adding a module = one file + one import line.
- Unknown `event_type` → generic card, generic icon, not clickable. Never crash on a type the frontend hasn't seen.
- If typed payloads are exported (§2.5), narrow with a type guard keyed on `event_type` inside the type's own `Card`.

**Data layer:** use the generated SDK functions (`timeline_list_events`, `timeline_histogram`, `timeline_list_event_types`, `timeline_get_event`) through whatever query/caching library the project already uses, with the list as an infinite query keyed by `(patientId, filters)`. Grouping and roll-ups are pure functions over the flattened pages (`groupEvents(events, tz) → Group[]`), unit-tested independently of React.

### 3.3 Component sketch

    TimelinePage
    ├─ TimelineFilterBar        (kind toggle, type multi-select, clear)
    ├─ TimelineFeed
    │  ├─ DateGroup (sticky header)
    │  │  ├─ RealWorldCard | GenericCard | TypeCard (from registry)
    │  │  └─ TechnicalRow | TechnicalRollup
    │  ├─ FeedSkeleton / EmptyState / ErrorState
    │  └─ LoadMoreSentinel
    ├─ DateNavigatorRail        (years → months, density from histogram)
    └─ EventDetailSheet         (deep link / secondary info)

## 4. Acceptance criteria

Backend:

- `record` twice for the same source/type yields one row with updated fields.
- `record` normalizes `DAY/MONTH/YEAR` dates to 12:00 UTC of the period start; `DATETIME` is stored as given.
- `record` rejects payloads that fail `payload_schema`, unknown keys, and `occurred_until < occurred_at`.
- Deleting a source object deletes its events; `remove(source)` works for soft deletes.
- `rebuild_timeline` on a DB with pre-existing documents creates exactly one event per document and removes events for documents no longer in `backfill()`.
- List endpoint: stable cursor pagination (no gaps/duplicates across pages when two events share `occurred_at`), filters combine, patient scoping enforced by the existing authorization.
- Histogram counts equal the list totals under the same filters.
- System check fails on a duplicate key, a key with wrong app prefix, or a bad `model` label.
- OpenAPI spec contains all four operations with the given `operation_id`s.

Frontend:

- Registered types open their target on click; unregistered types render generically and are not clickable.
- Roll-ups appear for ≥2 consecutive same-type technical events in a day and expand inline.
- Year-only and range events render with correct headers and labels.
- Filters and selected period are reflected in the URL and restored on reload.
- Feed works with keyboard only; loading/empty/error states are reachable in tests.

## 5. Out of scope for this iteration

Real-time push, full-text search over events, patient confirm/reject actions (phase 2 UI), extraction module event types. The schema and registry already support them; do not add speculative code for them.

## 6. Phase 2 note (for context only)

Extraction will register `kind=REAL_WORLD` types whose `model` is an `ExtractedFact`-like record carrying date + precision + confidence + evidence. `describe()` fills `title`/`description` from the fact and puts evidence (origin document id, page, snippet) into `payload`; `status` starts as `suggested`. Re-extraction calls `remove(origin_document)` for those types and re-records. Nothing in this guide needs to change for that.
