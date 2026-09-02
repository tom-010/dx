---
paths:
  - "**/admin.py"
---

## Admin (`apps/core/admin.py`)

A **dev-only** window on the rows, not a maintained UI. `ADMIN_ENABLED` defaults to `DEBUG`, so
in production `/admin/` does not resolve and the interactive API docs go with it (they need an
admin session to log in with). Administer production through `manage.py shell_as` /
`shell_admin`.

- **No admin classes.** Every app's `admin.py` is three lines — `register_all(models)` from
  `apps/core/admin.py`, which registers every model *that app defines* in the module with
  Django's default `ModelAdmin`. A new model appears without anyone maintaining a page for it,
  and the pghistory event models carry the app's label, so they show up too. It registers only
  the app's own models on purpose: `dir()` also sees whatever the module imported, and another
  app's model would either duplicate its page or raise `AlreadyRegistered` at boot.
- **Nothing is registered when the admin is off.** `register_all` returns early unless
  `settings.ADMIN_ENABLED`, so in production the registry holds none of our models — a page that
  cannot be reached is not built. Django still imports each `admin.py` (autodiscovery), which is
  why the guard lives in the function rather than at the six call sites.
- **`AdminTenantMiddleware` (`apps/core/middleware.py`) is what makes the pages resolve**: it
  runs `/admin/` requests inside the logged-in staff user's tenant context (tenant == user, so
  a staff user browsing the admin is a tenant browsing their own rows). Without it `OwnedManager`
  raises and row-level security hides everything — `TenantMiddleware` only runs under `/api/`.
  Kept a separate class on purpose: that one trusts a bearer token and nothing else, which is
  what stops a session cookie from ever authenticating the API.
- **Known sharp edges, accepted because nobody administers data here.** The pages are Django's
  defaults, so: the delete button issues a real `DELETE` and the `no_hard_delete` trigger turns
  it into a 500 (deleting is `obj.soft_delete()`); the event tables are append-only, so saving
  one of their change forms fails the same way; `User` gets a plain form, meaning the password
  field is a raw hash box, and deleting a user here would report success and then fail at the
  foreign key on commit (`manage.py delete_tenant` is the working path). Soft-deleted rows are
  simply invisible — `objects` hides them and there is no restore action any more.
- **The aggregate events page** (`pghistory.admin`) needs no tenant column of its own: every
  event table mirrors an `OwnedModel` and carries a real `owner_id` with the `tenant_isolation`
  policy, so **without a tenant context it returns nothing, not everything**.
  `PGHISTORY_ADMIN_ALL_EVENTS = False` — it unions *every* event table, so it stays blank until
  filtered. `PGHISTORY_BASE_MODEL` names `apps.core.history.Event` so every event table gets the
  same column set that union depends on.
- **Superusers see their own tenant, like everyone else.** There is no cross-tenant admin access
  and no second database alias: `manage.py shell_admin --reason '…'` is the audited way to read
  across tenants.
- `apps/core/tests/test_admin.py` is a smoke test — every registered changelist returns 200 (it
  would be a 500 without the tenant context) and one tenant's rows do not appear in another's.
- Django's built-in "History" button points at `LogEntry` and is unrelated to the version
  history; the real one is the SPA's revision page (`GET /api/history/{resource}/{id}`).
