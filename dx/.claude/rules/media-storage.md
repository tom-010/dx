---
paths:
  - "**/backend/apps/documents/**"
  - "**/backend/apps/gallery/**"
  - "**/backend/config/media.py"
  - "**/backend/apps/core/storage.py"
---

## Media files (stored in S3, served by Django)

Rule: **bytes live in the object store, every URL a client sees is a Django URL.** No presigned
S3 links, no public bucket, no CDN in front of the store — one origin (WhiteNoise-style), the
store stays private, and the bundled compose works without a browser-reachable `s3` host.

- Storage: Django's default storage (`STORAGES["default"]`) is `config.media.S3MediaStorage`
  (django-storages `S3Storage` + boto3) whenever `MEDIA_STORAGE=s3` (the default);
  `MEDIA_STORAGE=local` uses `config.media.LocalMediaStorage` (`FileSystemStorage` under
  `backend/media`). Tests always use local (`settings_test.py`), with `MEDIA_ROOT` pointed at a
  tmp dir per test. Models just declare `FileField`/`ImageField` — never pick a storage per field.
- Serving (`config/media.py`): `MEDIA_URL = /media/` is a Django route,
  `path("media/<path:path>", serve_media)` in `config/urls.py` (before the SPA catch-all, which
  excludes `media/`). `serve_media` verifies the signature, opens the key on the default storage
  and streams it as a `FileResponse` (inline, content type from the file name,
  `Cache-Control: private, max-age=MEDIA_LINK_MAX_AGE`); 403 JSON without a valid link, 404 JSON
  for a missing object.
- Links: both storage classes share `SignedMediaUrlMixin`, so **`field.url` is the only way to
  link to a file** and yields `/media/<key>?sig=…` — signed with `SECRET_KEY`
  (`django.core.signing`, salt `media.url`), valid for `MEDIA_LINK_MAX_AGE` (1 h). Browsers
  fetch them with plain `<img src>` / `<a href>` (no bearer header), which is why the route is
  public-but-signed instead of authenticated. In the SPA wrap them in `apiUrl()`
  (`src/lib/custom-fetch.ts`) — a no-op on the web, absolute for the Capacitor build. Put `file.url` into API schemas when a client
  needs a file (`resolve_*` on the ninja schema); never hand out storage keys or bucket names.
- Documents keep their own `GET /api/documents/{id}/download?sig=` (`DocumentOut.download_url`):
  same signing idea, but forces a download with the original file name; the payload is
  `[document id, owner id]` so the view can open that owner's tenant context (links signed
  before this change stop working — they expire within the hour anyway). Use `/media/…` for
  inline display and any other model's files.
- Scale note: Django streams every byte (gunicorn workers are busy for the duration). Fine for
  this app's file sizes; if it ever hurts, the seam is `SignedMediaUrlMixin.url()` — switch it
  to presigned store URLs (needs a public endpoint setting) without touching models or clients.
- Access control is per link, not per user: anyone holding an unexpired link can fetch the file.
  If files ever become user-scoped, sign the user id in as well and check it in `serve_media`.
- Tests: `apps/core/tests/test_media.py` (signed link works anonymously, unsigned/foreign/expired
  → 403, missing → 404, SPA catch-all does not swallow `/media/`).

### Object store (S3-compatible)

- Dev store: the compose `s3` service — **RustFS** (`rustfs/rustfs`, Apache-2.0 MinIO drop-in;
  MinIO stopped publishing community images in 2025). S3 API on host port **9100**, web console on
  **9101**, credentials `dx` / `dxdxdxdx`, data in the `s3data` volume. Ports 9000/9001 are
  deliberately not used (clash with Hadoop etc. on dev machines). Env: `S3_ENDPOINT_URL`
  (`http://localhost:9100`; empty = real AWS), `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET`
  (`dx-media`), `S3_REGION`. Path-style addressing + SigV4 are set in `settings.py`; keep them
  for any MinIO-like server.
- `manage.py ensure_bucket` (`apps/core/management/commands/`, logic in `apps/core/storage.py`)
  creates the media bucket (`S3_BUCKET`) and the backup bucket (`S3_BACKUP_BUCKET`) if missing
  and enables **bucket versioning**; idempotent. `./scripts/db.sh`
  runs it after the containers are healthy and `docker/entrypoint.sh` before `migrate`, so a
  fresh checkout/container is ready without manual steps. Do not create buckets anywhere else.
- Keys are `upload_to` + the upload name (`documents/<owner id>/%Y/%m/<name>`,
  `apps.core.models.owned_upload_path` — one prefix per tenant); `file_overwrite=False`
  makes django-storages append a random suffix on a clash, so files never share or overwrite a
  key. **Documents are the exception**: their files are `Blob` rows, content-addressed
  (`documents/<owner id>/blobs/ab/cd/<sha256>`, `apps.documents.models.blob_upload_path`) and
  deduplicated per tenant by `(owner, sha256)` — the 2026-08-28 decision against app-level
  dedup was reversed for them by the documents brief (2026-09-02), because a snapshot pins the
  exact bytes it was extracted from and the extractor's raw output is stored the same way. No
  other app dedups: a store-level mechanism is still where a general one would belong.
  A `Document` dedups **once more, a layer up**: `Document.md5` is unique per active row per
  owner, so re-uploading a file the tenant already holds gives the document that is already
  there rather than a second one over the same blob (`api.store_documents`). Two hashes, two
  questions: the blob's sha256 is "are these the same bytes", the document's md5 is "have I
  already filed this file" — and MD5 because it is the one a person can reproduce by hand.
- Versioning: deleting a document writes a delete marker, the previous version stays (recoverable
  in the console / `list_object_versions`); nothing in the app restores versions yet, and there
  is no lifecycle rule expiring old versions — add one before the store gets big.
- Integration test against the real store: `uv run pytest -m slow apps/documents/tests/test_s3.py`
  (skips when the store is down; uses a throwaway bucket). Everything else stays hermetic.
- Backups: database dumps go to the `dx-backups` bucket (see `.claude/rules/backups.md`); the uploaded objects
  themselves live in the `s3data` volume / media bucket — back up the store, not the app.
