
# Architektur & Tech-Stack — Datenmanagement-App

**Plattformen:** Web, Android, iOS · **Stand:** 28.08.2026 · **Status:** entschieden (offene Punkte in Abschnitt 10)

## 1. Zielbild und Leitplanken

Eine React-SPA als gemeinsame Codebasis für alle drei Plattformen: im Web direkt von Django ausgeliefert (WhiteNoise + CDN), auf Android/iOS als Capacitor-App verpackt. Das Backend ist Django und trägt ~90 % der Komplexität. Typsicherheit läuft end-to-end über eine generierte OpenAPI-Pipeline. Offline-Fähigkeit gibt es nur nativ; die Web-Version ist online-only.

Kriterien hinter allen Entscheidungen, in Prioritätsreihenfolge:

1.  **Dev-Speed** — kleines Team, schnelle Iteration.
2.  **Boring & future-proof** — 20-Jahre-Horizont: Foundation-/Community-Projekte, offene Specs, kein Rug-Pull-Risiko, jede Abhängigkeit ersetzbar halten.
3.  **Vorhandene Meisterschaft nutzen** — Python/Django im Backend, TypeScript dort, wo es unangefochten ist: im Frontend.
4.  **Enge Typ-Kopplung** Frontend ↔ Backend (RPC-Gefühl) — aber ohne Deployment-Kopplung an ein Fullstack-Framework.

## 2. Stack auf einen Blick

| Slot                | Entscheidung                                                                      |
|---------------------|-----------------------------------------------------------------------------------|
| Frontend            | Vite + React + TypeScript (SPA, bewusst kein SSR)                                 |
| Routing             | TanStack Router mit `autoCodeSplitting`                                           |
| Server-State        | TanStack Query (Hooks von orval generiert)                                        |
| Client-State        | Zustand (sparsam, nur echter Client-State)                                        |
| UI                  | Tailwind + shadcn/ui                                                              |
| Tabellen            | TanStack Table (AG Grid nur, falls Excel-artiges Editing/Pivoting nötig wird)     |
| Formulare           | react-hook-form + generierte Zod-Schemas                                          |
| Backend             | Django + **django-ninja** (Pydantic, RPC-förmige Endpoints)                       |
| Datenbank           | PostgreSQL                                                                        |
| API-Vertrag         | OpenAPI (aus ninja) → **orval** → typisierter TS-Client                           |
| Auth                | Token-basiert, einheitlich für Web + Native; Authorization in der Service-Schicht |
| Background-Jobs     | Postgres-basiert, kein Redis (Auswahl offen, Abschnitt 10)                        |
| Offline (nur nativ) | Eigener Sync in Django: Pull/Push, Outbox, LWW (Abschnitt 6)                      |
| Mobile-Shell        | Capacitor; Push via `@capacitor/push-notifications` + FCM (→ APNs auf iOS)        |
| OTA-Updates nativ   | Capgo (JS/Assets ohne Store-Review)                                               |
| File-Storage        | S3-kompatibel (R2/S3), presigned URLs aus der API                                 |
| Serving Web         | Django + **WhiteNoise**, CDN davor (z. B. Cloudflare)                             |
| Hosting             | Docker-Image auf Railway/Fly/Hetzner + Managed Postgres                           |
| Tests               | pytest + pytest-django, mypy im CI; Vitest + Testing Library; Playwright e2e      |
| Tooling             | Monorepo `backend/` + `frontend/`; uv + ruff (Python), pnpm + Biome (TS)          |

## 3. Begründungen der Kernentscheidungen

**React + Capacitor statt React Native.** Dev-Speed-Priorität plus Datenmanagement-Domäne: Browser-Dev-Loop ohne Xcode/Android Studio im Alltag, und das Web-Ökosystem für dichte Daten-UIs (TanStack Table, Formulare, Import/Export) ist React Native deutlich überlegen. Einzige native Anforderung ist Push — das löst der Capacitor-Wrapper. React Native Web wäre für die Web-Version das schwächste Glied gewesen.

**Django statt TypeScript-Backend.** Das Backend trägt 90 % der Komplexität, die App ist datenzentrisch (pandas/polars-Territorium), und Django ist die einzige Batteries-included-Option, die den 20-Jahre-Beweis bereits erbracht hat. Vorhandene Django-Meisterschaft ist ein echter Dev-Speed-Multiplikator. Die Sprach-Kopplung ans Frontend wird durch Vertrags-Kopplung ersetzt (OpenAPI-Codegen) — damit ist die Backend-Sprache ein freier Parameter.

**django-ninja statt DRF.** DRF-Serializer sind praktisch untypisiert und damit der klassische Stolperstein für das E2E-Typing-Ziel. ninja ist eine dünne Schicht über Pydantic mit automatischer OpenAPI-Spec — kleines API-Surface, notfalls ein Wochenende Migration.

**OpenAPI statt tRPC.** OpenAPI ist eine Spec unter der Linux Foundation: sprachneutral, tool-unabhängig, in 20 Jahren noch lesbar. tRPC wäre eine Wette auf eine einzelne Library. Wichtig: OpenAPI erzwingt kein „entkoppeltes REST" — Endpoints werden RPC-förmig geschnitten (`POST /orders/import`, `POST /reports/generate`). Ehrliches Rest-Delta zu tRPC: ein Codegen-Schritt statt purer Inferenz, Go-to-Definition landet in generierten Typdateien.

**Modularer Monolith.** Ein Deployable. Schichtung: ninja-Router pro Feature-Modul → Service-Schicht (plain typed Python — hier leben die 90 %) → ORM. Keine Microservices; Modulgrenzen bei Bedarf per Lint (z. B. import-linter) statt per Netzwerk erzwingen. Kernprinzip: Geschäftslogik ohne Framework-Imports, damit jede Einzelentscheidung von „existenziell" auf „lästig" herabgestuft wird.

**Offline nur nativ, Web online-only.** Mobile hat den Code ohnehin im Binary (Capacitor packt `dist` in .apk/.ipa). Der Datensync wird selbst gebaut, weil der erwartete Konfliktbedarf klein ist und Permissions + Validierung so genau einmal existieren — in Django.

**WhiteNoise statt separatem Static-Host.** Ein Origin, ein Deploy, maximal boring; die Web-Version braucht damit kein CORS (nur die Capacitor-Origins, siehe Abschnitt 8). Bewusster Trade-off: Frontend- und Backend-Deploy sind gekoppelt. Für native OTA entkoppelt Capgo trotzdem vom Store-Rhythmus.

## 4. Typ-Pipeline (End-to-End)

Der Fluss: **Pydantic-Schemas in ninja → OpenAPI-Spec → orval → TS-Typen + TanStack-Query-Hooks (+ Zod-Schemas)**.

- Jede Operation deklariert `response=Schema`; Rückgaben sind Schemas, keine nackten dicts. `ModelSchema` hält Model und Schema drift-frei.
- orval läuft in der Entwicklung im **Watch-Mode**: Feld im Pydantic-Modell umbenennen → betroffene Frontend-Stellen werden in Sekunden rot.
- **CI-Job:** Spec regenerieren und gegen den eingecheckten Client diffen → Vertragsdrift ist strukturell ausgeschlossen.
- Generierter Code (`frontend/src/api/`) wird nie von Hand editiert.
- Falls je Dritt-Konsumenten nötig: dieselbe OpenAPI-Spec, ein kleiner stabiler Ausschnitt.

## 5. Python-/Django-Typsicherheit (Checkliste)

**Checker-Setup (größter Hebel):**

- CI: **mypy `strict` + django-stubs** (+ `django-stubs-ext`) — das einzige Setup, das ORM-Magie wirklich versteht. Editor: **basedpyright + django-types** (schnell). Doppelgleisig fahren.
- ruff mit `ANN`-Regeln (fehlende Annotationen = Lint-Fehler); `warn_unused_ignores`; für Bestandscode mypy-baseline oder per-Modul-Strictness.

**ORM härten:**

- Custom QuerySets generisch typisieren (`models.QuerySet["Order"]`), Manager explizit annotieren.
- `values()`/`only()` in Logik meiden (Any-Löcher) — Instanzen oder früh auf Schemas/Dataclasses mappen.
- `JSONField` → **django-pydantic-field**. `TextChoices` statt String-Konstanten, Verzweigungen mit `match` + `assert_never` erschöpfend.
- **`NewType`-IDs** (`OrderId = NewType("OrderId", int)`) gegen vertauschte ints zwischen Entitäten.

**Grenzen abdichten:**

- Pydantic überall, wo Daten reinkommen: ninja-Schemas, **pydantic-settings** statt `os.environ`, Task-Payloads, externe API-Responses.
- Typisiertes `request.auth` aus der ninja-Auth-Klasse nutzen statt `request.user` überall zu narrowen.

**Sprache & Datenschicht:**

- Decorators mit `ParamSpec`/`Concatenate`; `Protocol` für Service-Interfaces; Services ohne `request`-Durchreichen und ohne `**kwargs`.
- pandas: **pandas-stubs + pandera** (bei polars: pandera oder patito). DataFrames an Systemgrenzen validieren wie API-Input.
- **beartype**/typeguard in Dev und Tests als Runtime-Netz; `types-*`-Stubs bzw. eigene Mini-Stubs statt globalem `ignore_missing_imports`.

**Akzeptierte Restlöcher:** `reverse()`-Strings (URL-Namen als zentrale Konstanten), Signals (vermeiden → explizite Service-Aufrufe), Templates (irrelevant, API-only).

## 6. Offline-Architektur (nur native Apps)

**Entscheidung 0 (Produktfrage, offen):** Welche Entitäten müssen offline *editierbar* sein? Sync-Scope minimal halten — jede nicht gesyncte Tabelle erzeugt null Konflikte; alles andere bleibt online-only über die normale API.

**Falls read-only genügt:** `persistQueryClient` von TanStack Query auf SQLite persistieren, „Stand: vor X"-Banner, Mutationen offline deaktivieren. Aufwand: Tage, nicht Wochen.

**Read-write — DIY-Sync in Django (gewählter Weg):**

- Abstraktes `SyncModel`: client-generierte **UUIDv7-Primärschlüssel** (Offline-Creates kollisionsfrei), `deleted_at`-Tombstones statt harter Deletes, `row_seq` (BigInt) per Postgres-Trigger aus globaler Sequence als robuster Pull-Cursor.
- `GET /sync/pull?since=<cursor>` → geänderte Rows + Tombstones, gefiltert über **dieselben Permission-Querysets wie die normale API** (Authz existiert genau einmal).
- `POST /sync/push`: Batch aus `{mutation_id, entity, op, payload, base_version}`. Idempotenz über mutation_id-Tabelle; Anwendung durch die Service-Schicht inklusive Validierung.
- Konfliktpolicy: Default Last-Write-Wins auf Zeilenebene mit **Server**-Zeit; alternativ Reject + aktuelle Row zurück zum Mergen. Pro Entität festlegen; **UI für abgelehnte Änderungen einplanen** — Server-Validierung wird offline erfasste Daten zurückweisen.
- Client: `@capacitor-community/sqlite`, Outbox-Tabelle für lokale Mutationen; Sync-Loop bei App-Start, Foreground, Netzwechsel (Capacitor Network) und nach Push-Notification.
- Bonus: Die Sync-Endpoints sind ninja-Schemas → laufen durch die orval-Pipeline, der Sync-Client ist automatisch typisiert.

**Feste Regeln:** Nie Client-Uhren trauen. `schema_version` in jedem Push + Force-Update-Pfad via Capgo (uralte Clients dürfen keine kaputten Mutationen schieben). Token-Ablauf offline tolerieren (lokal weiterarbeiten, Re-Auth beim Reconnect). Attachments: Capacitor Filesystem + eigene Upload-Queue.

**Plan B:** PowerSync, falls Partial-Sync-Umfang oder Konfliktkomplexität real wachsen (liest Postgres per logischer Replikation; Writes weiter über Django). Watchlist: ElectricSQL + TanStack DB.

## 7. Web-Bundle-Strategie

Ziel: Initial-Load **~200–400 KB (Brotli)**, unabhängig von der Repo-Größe. Kein Web-Nutzer lädt je „die App" — nur seinen ersten Screen; der Rest kommt on demand vom CDN.

- `rollup-plugin-visualizer` dauerhaft im Build; optimiert wird nur nach Messung (fast immer dominieren 3–4 Pakete: Excel, Charts, Editor, Icons).
- TanStack Router `autoCodeSplitting: true` → ein Chunk pro Route; `defaultPreload: 'intent'` gegen fühlbare Ladezeiten.
- Megabyte-Fresser (exceljs, PDF-Generierung, Chart-Libs, Rich-Text-Editor) per `await import()` erst im Klick-Handler laden.
- **Sync-/SQLite-Code strikt nativ halten:** dynamischer Import hinter `Capacitor.isNativePlatform()`, oder sauberer per `VITE_PLATFORM`-Flag mit zwei Builds (Tree-Shaking entfernt ihn im Web komplett). Ein statischer Import würde ihn ins Web-Bundle ziehen.
- Import-Hygiene: keine eigenen Barrel-Files, Icons einzeln importieren, dayjs/date-fns statt moment, Locales gezielt laden.
- `manualChunks`: stabiler Vendor-Chunk (React/TanStack) → Deploys invalidieren nur wirklich geänderte Chunks.
- CI-Guardrail: **`size-limit`** mit hartem Budget fürs Initial-Bundle (z. B. 300 KB Brotli) + dependency-cruiser-Regel („exceljs nur unter `features/export`"). Der klassische Unfall ist ein Top-Level-Import einer schweren Lib in einer Shared-Util.
- **Kein Service Worker** (Web ist nicht offline) — erspart die Stale-Version-Bug-Klasse gleich mit.

## 8. Serving & Deployment (WhiteNoise-Variante)

**Ein Artefakt:** Das Docker-Image enthält Django *und* `frontend/dist` als Static Files.

- Build-Pipeline: `pnpm build` → dist in die Static-Verzeichnisse (collectstatic) → Docker-Image → Railway/Fly/Hetzner; Managed Postgres.
- WhiteNoise mit Brotli-Kompression. **`WHITENOISE_IMMUTABLE_FILE_TEST` an Vites Hash-Muster anpassen**, damit gehashte Assets mit `immutable`-Cache-Headern ausgeliefert werden; `index.html` no-cache — sonst kleben Nutzer auf alten Deploys.
- **SPA-Fallback:** Catch-all-View (nach `/api`, `/admin` und Static-Routen) liefert `index.html`, sonst 404 bei Deep-Links.
- CDN davor (z. B. Cloudflare) cacht die gehashten Assets am Edge.
- **API-URLs:** Der Web-Build ruft relativ `/api/...` auf (same-origin — kein CORS für Web). Der Native-Build bekommt die absolute URL per Env/Build-Flag.
- **CORS/CSRF sind trotzdem Pflicht — für Capacitor:** Die App läuft von `capacitor://localhost` (iOS) bzw. `https://localhost` (Android). Beide Origins in django-cors-headers und `CSRF_TRUSTED_ORIGINS` eintragen. Token-Auth hält das Cookie-/CSRF-Thema über beide Plattformen einfach.
- Ops: strukturiertes Logging (structlog), Sentry; OpenTelemetry erst bei Bedarf.
- Native Releases: Stores nur für Binary-Änderungen (Capacitor-Plugins etc.), **Capgo** für JS/Asset-Updates im Web-Deploy-Takt.

## 9. Repo-Layout

    repo/
      backend/                 # Django · uv, ruff, mypy strict
        apps/<feature>/        # Modul: ninja-Router + Services + Models
        config/
      frontend/                # Vite-SPA · pnpm, Biome
        src/routes/            # TanStack Router, dateibasiert
        src/features/
        src/api/               # von orval generiert — nie von Hand editieren
      orval.config.ts
      docker/
      .github/workflows/       # mypy, pytest, ruff/biome, spec-diff, size-limit, playwright

## 10. Offene Punkte & nächste Schritte

1.  **Task-Queue wählen:** Postgres-basiert bevorzugt (kein Redis — analog zur pg-boss-Logik): procrastinate oder das `django.tasks`-Framework (ab Django 6) mit DB-Worker. Celery nur, falls Durchsatz es erzwingt.
2.  **Auth konkretisieren:** Token-Schema (z. B. ninja `HttpBearer` + eigenes Token-Modell oder knox), Refresh-Strategie inklusive Offline-Fall.
3.  **Offline-Scope festlegen (Produkt):** Welche Entitäten offline editierbar? Danach Konfliktpolicy je Entität + Rejection-UI.
4.  **Erster vertikaler Slice (1 Woche):** Import → Validierung → Persistenz → Export, inklusive orval-Loop und einem Background-Job. Validiert Typ-Pipeline und Dev-Loop, bevor mehr gebaut wird.
5.  **Reihenfolge:** Als reine Vite-Web-App starten; Capacitor erst andocken, wenn die Kern-Screens stehen (das Wrappen von `dist` ist ~ein Tag Arbeit).
