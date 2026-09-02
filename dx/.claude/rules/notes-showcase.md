---
paths:
  - "**/backend/apps/notes/**"
  - "**/frontend/src/routes/notes.tsx"
---

## `apps/notes` — showcase, safe to delete

A note-taking app that exists to *demonstrate* versioning and lineage end to end, not because
the product needs notes. It is deliberately self-contained.

- `Note(OwnedModel)` — title, body, and `tags` as a plain comma-separated string, normalised on
  write (`api.normalize_tags`) so that retyping the same set in a different order is not a
  change in the note's history. Deliberately *not* the shape `apps/datasets` uses: an owned,
  versioned `Tag` model with a join table is what you want when tags are shared, renamed or
  counted; a string is what you want when a tag is just a label on one note.
- One derivation, `merge` (many → one), recording an edge to the **version** of each source it
  read (`merge_notes_for` in `apps/notes/api.py`). Merging alone is enough for a directed acyclic
  *graph*: a note
  merged into two others branches, and merging two merged notes closes a diamond. The sources are
  left alone — a merge adds a note, it does not consume any.
- `GET /api/lineage/{resource}/{id}?depth=` (`apps/core/lineage.py::graph`, `apps/core/api.py`)
  walks that graph from any object, in **both** directions: a sibling and a co-parent are only
  reachable by going up and then down again. `depth` on a node is a signed generation (a source
  is -1, something derived +1, a sibling 0), which is what lets the page draw it in rows.
  The endpoint is generic — it works for datasets too, which have document→dataset edges.
- Frontend: `src/routes/notes.tsx` (create, edit in place, select-and-merge) and
  `src/routes/lineage.$resource.$objectId.tsx` (the graph as plain SVG — no layout library for
  one screen; a stale edge is dashed and red).

**To delete it**: remove `apps/notes/`, `frontend/src/routes/notes.tsx`, its nav entry in
`__root.tsx`, the `notes` entries in `INSTALLED_APPS` and `config/api.py`, the `notes` resource
in `apps/core/tests/test_ownership.py` and the two `notes.*` lines in `test_tenancy.py`'s erasure
and `TENANT_APPS` assertions; then `manage.py history_schema --write` and
`./scripts/sync_schema.sh`. Keep `apps/core/lineage.py::graph` and
`/api/lineage/...` — those are general infrastructure, not part of the showcase.
