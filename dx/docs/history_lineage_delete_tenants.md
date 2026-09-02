# Where data comes from: lineage, history, soft delete, tenancy

Four mechanisms, one goal: **for any row, be able to say where it came from — and still be able
to say it a year later.** Each one exists because the previous one is not enough on its own.

## 1. Lineage — where did this come from?

A derived row records what it was built from — on the write itself:

```python
report = Report.create(text=..., operation="monthly report", sources=[rates, totals])
report.save(operation="rebuild report", sources=[rates])          # an edit, likewise
with deriving(rates, totals):                            # every write in the block
    Chart.objects.create(spec=...)
```

Each source becomes an append-only edge (`apps/core/lineage.py`), written in the same transaction
as the row and pinned to the version the write produced. `sources=[]` is how a write says it was
built from nothing — deliberately longer than saying what it was built from. Read it back with
`obj.sources()` and `obj.derived()`. Edges carry the call stack and the build that recorded them,
so "which code claimed this" is answerable too.

## 2. History — but the sources have changed since

A foreign key answers "who is the author *now*". That is the wrong question: a report built in
March was built from the numbers as they stood in March. So an edge points at a **version**, not
at a row — and versions have to exist, which is what `apps/core/history.py` provides. Every write
to a tracked table is mirrored into an append-only event table by a **database trigger**, so a
`.update()`, raw SQL and a data migration are all captured; application code cannot bypass it.

`obj.history()` gives the chain; `version.to_object()` rebuilds the row as it was;
`version.is_current()` says whether it has moved on. `lineage.stale_derivations(obj)` is the
payoff: *what would come out differently if rebuilt now*.

## 3. Soft delete — and nothing may disappear underneath it

A version chain with holes in it is not a chain. If a source could be deleted, the edges naming
its versions would dangle and the March report could no longer explain itself. So `delete()`
sets `deleted_at` and is an ordinary versioned write; `hard_delete()` is the explicit exception
(`apps/core/models.py`). A Postgres trigger refuses a real `DELETE`, which covers the paths
Python cannot: raw SQL, migrations, the deletion collector. Details: `docs/soft-delete.md`.

## 4. Tenancy — so how do you ever delete anything?

Because we keep everything, deleting a person's data has to be possible in one clean sweep.
**Tenant == user**: every feature row carries an `owner` (`OwnedModel`, `apps/core/models.py`),
and so does every event row and every lineage edge. One user's data is therefore a **disjoint
subgraph** — nothing crosses between tenants, by construction — and erasing it is walking the
tables of one owner (`apps/core/tenants.py`, `manage.py delete_tenant`; see
`docs/tenant-data.md`).

Isolation is enforced twice, because an application bug must not be able to leak or to strand
data: the ORM scope (`OwnedManager`, which raises outside a tenant context) and Postgres
row-level security (`apps/core/rls.py`), which is the actual guarantee.

## The shape of it

```
lineage edge ──points at──▶ version ──belongs to──▶ row ──owned by──▶ user
  (never deleted)        (append-only)        (soft-deleted only)   (erasable, wholly)
```

Read bottom-up it is the argument in reverse: a user can be erased completely *because* their
data is a disjoint subgraph; a row is never really deleted *because* versions must stay
resolvable; versions exist *because* an edge must name a state, not a row; edges exist *because*
we want to know where anything came from.

## Seeing it

`/explorer/` (development only, `apps/core/explorer.py`) walks all four: users → models → rows →
one version → one edge and the stack that recorded it. `manage.py lineage_demo` builds a graph
with the interesting shapes in it.

## Where the code is

| | |
|---|---|
| `apps/core/lineage.py` | edges, the graph, `stale_derivations` |
| `apps/core/history.py` | `@tracked`, event tables, `Version`, `history_context` |
| `apps/core/models.py` | `VersionedModel` (delete/hard_delete, version chain), `OwnedModel` |
| `apps/core/rls.py` | the row-level security policies |
| `apps/core/tenants.py` | export, summary, erasure of one tenant |
| `apps/core/revisions.py` | the revision page's data layer |

Rules with the invariants: `.claude/rules/versioning.md`, `.claude/rules/multitenancy.md`.
