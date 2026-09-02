# What one write records, and how

Every `save()` leaves four kinds of trace. Together they answer, for any row at any time: what
did it hold, which code wrote it, which step it was part of, what did it consume, and what did
the client send. The rows are what the explorer (`/explorer/`) walks.

| kind | table | one row per | written by |
|---|---|---|---|
| version | `<app>_<model>event` | write to a tracked row | database trigger |
| edge | `core_lineage` | (target version, source version) pair | `record_derivation()` |
| function source | `core_sourcesnippet` | distinct function body | first write that ran it |
| request | `core_requestrecord` | HTTP request that wrote something | first write of the request |

## The version row

The tracked row's columns as they stood, plus:

| column | what | how |
|---|---|---|
| `version`, `deleted_at` | position in the chain; soft-delete state | `bump_version` trigger; `delete()` is an UPDATE |
| `pgh_id`, `pgh_obj_id`, `pgh_label`, `pgh_created_at` | identity, row, insert/update, when | pghistory |
| `pgh_context_id` | the run: `source` (operation name, or `api`/`task`/`command`), `description` | `history_context()`; `save(operation=…)` opens one per write |
| `pgh_schema`, `pgh_archive` | tracked-field set at the time; dropped fields' values | `SCHEMA_TAG`; data migration |
| `pgh_stack` | the call stack — this project's frames only, outermost first | `dx.stack` setting → column default |
| `pgh_release` | the build (`APP_VERSION`, or the working tree's commit) | `dx.release` setting → column default |
| `pgh_request` | the HTTP request, if any | `dx.request` setting → column default |

Each stack frame: `file`, `line`, `func`, `code` (the executing line), `module`, `sha` (→ function
source), `first_line` (so the line can be found in the function).

## The edge

`sources=[…]` on a write, or a `deriving()` block, becomes one edge per source:

| column | what |
|---|---|
| `owner` | tenant — edges are owned rows |
| `target_type`, `target_pgh_id` | the *version* built |
| `source_type`, `source_pgh_id`, `source_obj_id`, `source_version` | the *version* consumed; obj/version denormalised so "what is stale" is an index scan |
| `pgh_context` | the run — the target version's, not the recording call's |
| `stack`, `release`, `request` | the same three as the version |

## The function source

`sha` (sha-256 of the text) → `text`, the whole function the frame was in, taken from the
interpreter (`co_firstlineno` … max of `co_lines()`, via `linecache`). Stored once per distinct
body, like a git blob; no git involved. Shared table — code is not tenant data.

## The request

| column | what |
|---|---|
| `owner` | tenant — a body is PII |
| `request_id` | django-structlog's id: the join key into the logs |
| `method`, `path`, `sent_query` | what was asked |
| `sent_headers` | as sent, with `Authorization`, `Cookie`, CSRF values replaced by `<redacted>` |
| `content_type`, `sent_body`, `body_size`, `body_status` | JSON only, ≤ 64 KB; `none` / `unreadable` (a consumed upload) / `too-large` / `invalid-json` say why not |

## The four mechanisms

1. **Trigger columns fed by transaction-local settings.** Python never inserts a version row, so
   the stack, release and request go through `set_config('dx.…', …, true)` and the columns'
   `db_default` reads them — exactly how pghistory passes its context. `save()` sets them right
   before the write (`lineage.declare_write_origin`) and blanks them after, so a bulk `.update()`
   or a migration records NULL rather than the previous write's values.
2. **Explicit lineage on every write.** `operation=` and `sources=` are required (`None` defers to
   the enclosing `history_context()` / `deriving()` block, `[]` means none); `record_sources()`
   turns them into edges in the same transaction. `Model.objects.create()` is refused.
3. **Stacks filtered at capture, by exclusion.** `is_ours()` asks the interpreter where *it* lives
   (`sys.prefix`, `sysconfig`, `site`); everything else is ours — every frame of it, minus the
   recording mechanism (`lineage.py`, `models.py`). No list of packages to maintain.
4. **Dedup that costs nothing on the hot path.** Function sources: a process-local set of committed
   shas → the shared cache (Redis) → one `INSERT … ON CONFLICT DO NOTHING`; both caches marked on
   commit only. Requests: one row per request, recorded on the first write, its id stamped on the
   rest. A request that writes nothing, or a write with no request, leaves nothing.

## Reading it back

`obj.history()` → `Version` (`.stack`, `.caller`, `.release`, `.request_id`, `.sources()`,
`.derived()`, `.to_object()`, `.is_current()`); `obj.sources()` / `obj.derived()` across all
versions; `Lineage.caller`; `lineage.stale_derivations(obj)`. The explorer shows all of it:
users → models → rows → a version → its edges → the request, with each frame's function folded
underneath.

## Where the code is

`apps/core/history.py` (Event columns, `history_context`, `Version`) · `apps/core/lineage.py`
(edges, capture, `declare_write_origin`, `deriving`) · `apps/core/source.py` ·
`apps/core/request_record.py` · `apps/core/models.py` (`save`/`create`) · `apps/core/explorer.py`.
Rules: `.claude/rules/versioning.md`. The why: `docs/history_lineage_delete_tenants.md`.
