---
paths:
  - "**/backend/apps/accounts/**"
  - "**/backend/config/api.py"
  - "**/frontend/src/lib/auth.ts"
  - "**/frontend/src/lib/custom-fetch.ts"
  - "**/frontend/src/routes/login.tsx"
---

## Auth (`apps/accounts/`)

- Everything lives in `apps/accounts/api.py`: the schemas, the JWT handling, `BearerAuth`,
  `current_user()` and the routes (there is no `auth.py` and no `services.py`).
- Every API operation requires `Authorization: Bearer <token>` — `BearerAuth` (ninja
  `HttpBearer`) is installed globally in `config/api.py`. Public operations opt out with
  `auth=None` (health/ready, login, register, signed document downloads, `/api/docs`). Outside the
  API, `/media/<key>?sig=` is public-but-signed (see `.claude/rules/media-storage.md`). `TenantMiddleware`
  (`apps/core/middleware.py`) resolves the same header before the view and opens the tenant
  context; `BearerAuth` reuses its result (verified exactly once) — see `.claude/rules/multitenancy.md`.
- Three token kinds, all resolved by `authenticate_bearer()`:
  1. JWT access tokens (HS256 with `SECRET_KEY`, claim `token_type=access`, stateless and
     therefore short-lived: `ACCESS_TOKEN_LIFETIME_MINUTES`, 15) from `POST /api/auth/login`
     (`{username, password}` → `{access_token, refresh_token}`). The refresh token is a second
     JWT (`token_type=refresh`, `REFRESH_TOKEN_LIFETIME_DAYS`, 30) whose `jti` is a
     `RefreshToken` row = the login session. `POST /api/auth/refresh` (`{refresh_token}`,
     public — the access token is expired by then) trades it for a new pair and revokes the
     old row (single-use); `POST /api/auth/logout` (`{refresh_token}`, public, always 204)
     revokes it. Expired/revoked/inactive user → 401 "Invalid or expired refresh token".
     Deliberately no reuse detection (ending every session when an old token shows up — two
     tabs refreshing at once would trigger it). Login purges the user's expired rows;
     `session_from_refresh_token()` returns `None` for anything spent. `GET /api/auth/me`
     returns the caller.
  2. Personal API tokens (`tk_…`, `ApiToken` model, never expire) for scripts/CI:
     `GET/POST /api/auth/api-tokens`, `DELETE /api/auth/api-tokens/{id}`.
  3. `API_FIXED_TOKEN` from the environment → acts as the first superuser (CI without DB state).
- `POST /api/auth/register` only works with `REGISTRATION_OPEN=true` (403 otherwise).
- Inside operations use `current_user(request)` (`apps/accounts/api.py`); helper functions take
  a `User`, never a request, and scope owned data with it (see "Data model conventions").
  Every other app imports it as `from apps.accounts.api import current_user`.
- Files that a browser fetches via plain `<a href>`/`<img src>` (no header) use signed, expiring
  URLs: `DocumentOut.download_url` carries `?sig=` (`documents/api.py::sign_download`),
  every `FileField.url` does too (`config/media.py::media_url`).
- Frontend: `src/lib/auth.ts` keeps the pair (localStorage `dx.access_token` /
  `dx.refresh_token`, `useAccessToken()`; the `storage` event keeps open tabs in sync).
  `custom-fetch.ts` adds the header; on a 401 it calls the generated `refreshToken()` once
  (single-flight — requests failing together share it, the refresh token being single-use),
  stores the new pair and retries the request. Only when the refresh is rejected too are the
  tokens dropped and `routes/__root.tsx` redirects to `/login` (`routes/login.tsx`,
  `useLogin()`); a rejection because another tab already rotated the pair falls back to that
  tab's tokens (`reloadTokens()`). Network errors keep the session (offline: the refresh
  happens at the next 401 after reconnecting). Logout (`__root.tsx`) posts the refresh token
  to `logout()`, then clears tokens + query cache.
- Dev: `manage.py createadmin` (admin/admin), `manage.py token [-m MINUTES]` prints an access
  JWT for curl; anything unattended should use a personal API token instead.
- The user model is our own: `apps.accounts.models.User` (`AUTH_USER_MODEL = "accounts.User"`,
  `AbstractUser` + UUIDv7 id). Import it from there (or `get_user_model()`), never from
  `django.contrib.auth.models`; extend it in place instead of adding a profile table.
