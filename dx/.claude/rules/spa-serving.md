---
paths:
  - "**/backend/config/spa.py"
  - "**/backend/config/static.py"
  - "**/backend/config/urls.py"
  - "**/frontend/vite.config.ts"
---

## Serving the SPA from Django (WhiteNoise)

- Vite builds with `base: "/static/"`; `FRONTEND_DIST` (default `../frontend/dist`) is in
  `STATICFILES_DIRS`, so `collectstatic` copies `index.html` + `assets/` into
  `backend/staticfiles/`, and WhiteNoise serves them under `/static/` (gzip + brotli).
- `config/urls.py` ends with a catch-all that serves `staticfiles/index.html` for every path not
  starting with `api/`, `admin/`, `static/`, `media/` — deep links work, HTML is
  `Cache-Control: no-cache`.
  Without a build it returns 503 with a hint.
- Hashed Vite assets (`name-<8 chars>.ext`) get immutable cache headers
  (`config/static.py::vite_immutable_file`); storage is `CompressedStaticFilesStorage` (no
  Django manifest hashing — Vite already hashes).
- In dev nobody uses this: Vite on :5173 serves the app and proxies `/api`. WhiteNoise runs in
  finder/autorefresh mode when `DEBUG=true`, and `/` on :8000 is the dev home page instead of the
  catch-all — a link list to the docs, explorer, admin and the Vite server, behind the admin
  login, with a POST-only logout at `/logout/` (`config/home.py`, mounted while
  `DEV_HOME_ENABLED`, which defaults to DEBUG).
