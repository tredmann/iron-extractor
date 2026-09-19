# Docling Extraction API — Design

**Date:** 2026-09-19
**Status:** Approved for implementation

## Purpose

An HTTP API that converts documents with [docling](https://github.com/docling-project/docling)
and ships as a single Docker container. A caller uploads a document, receives a UUID
immediately, and retrieves the conversion results from separate routes once the job
finishes.

Conversion is slow — seconds to minutes for OCR-heavy PDFs — so the API is asynchronous
by design. The upload route never blocks on a conversion.

## Success criteria

- `POST` a PDF, receive a UUID in under a second.
- Poll one route to learn status; on success that same response links every artifact.
- Retrieve markdown, the full DoclingDocument JSON, each table as CSV or HTML, and each
  extracted picture.
- `docker compose up` produces a working service with no network access at runtime.
- Swagger UI at `/docs` is sufficient to integrate against without reading the source.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Job execution | In-process background workers | Single container; no Redis or worker fleet to operate |
| Concurrency | Bounded `asyncio.Queue` + N worker threads | Models load once; memory stays flat; torch releases the GIL for most inference |
| Persistence | Disk volume + SQLite index | Survives restarts, no extra service, trivial to back up or wipe |
| Outputs | Markdown, DoclingDocument JSON, tables (CSV + HTML), pictures | All four requested |
| Request options | Four bounded knobs | Useful control without exposing docling's whole surface |
| Model weights | Baked into the image at build time | Fast cold start, reproducible, works offline |
| Auth | Static API keys via `X-API-Key` | Cheap now, painful to retrofit |
| Docs | FastAPI OpenAPI with a declared security scheme | Integrators need the auth and error cases visible |

## Architecture

One FastAPI process with three layers that do not know about each other.

### `JobStore` — persistence

SQLite. Pure persistence; imports neither docling nor FastAPI.

```
create(id, filename, options) -> Job
get(id) -> Job | None
mark_running(id) / mark_done(id, manifest) / mark_failed(id, type, message)
delete(id)
recover_interrupted() -> list[str]    # startup sweep, returns ids to re-enqueue
```

One table `jobs`:

| column | type | notes |
|---|---|---|
| `id` | TEXT PK | uuid4 |
| `status` | TEXT | `queued` / `running` / `succeeded` / `failed` |
| `filename` | TEXT | original upload name |
| `options` | TEXT | validated options as JSON |
| `created_at` / `started_at` / `finished_at` | TEXT | ISO-8601 UTC, nullable |
| `error_type` / `error_message` | TEXT | nullable |
| `manifest` | TEXT | artifact manifest JSON, nullable |

### `ConversionService` — docling

Takes an input path and validated options, runs the conversion, writes every artifact
into the job directory, returns the manifest. Knows docling and the filesystem; knows
nothing about jobs, queues, or HTTP.

This is the only module that imports docling, so a docling upgrade touches one file.

### `WorkerPool` — execution

A bounded `asyncio.Queue` plus N threads. Each thread pulls a job id, marks it running,
calls `ConversionService`, and records the manifest or the failure. Knows the store and
the service; knows nothing about HTTP.

### API layer

Routes, pydantic schemas, and an API-key dependency. Touches only `JobStore` and
`WorkerPool.enqueue`.

### Lifespan

**Startup:** open SQLite and run migrations; build one `DocumentConverter` per worker
thread from the baked-in weights; run `recover_interrupted()`; re-enqueue recovered
`queued` jobs; start worker threads; mark the app ready.

**Shutdown:** stop accepting new work, allow in-flight conversions a grace period to
finish, close the database.

### Module layout

```
app/
  main.py              # app factory, lifespan wiring
  config.py            # pydantic-settings
  api/
    routes.py          # endpoints
    deps.py            # auth dependency, injection
    schemas.py         # request/response models
    errors.py          # error envelope, exception handlers
  jobs/
    store.py           # JobStore
    pool.py            # WorkerPool
    models.py          # Job, JobStatus
  conversion/
    service.py         # ConversionService
    converters.py      # per-thread DocumentConverter cache
    artifacts.py       # artifact writing + manifest
  storage/
    paths.py           # job directory layout
```

## Data flow

```
POST /v1/documents  →  auth → uuid4 → save upload to /data/jobs/<id>/input/
                    →  store.create(status=queued) → pool.enqueue(id)
                    →  202 {id, status, links}        [queue full → 503 + Retry-After]

worker thread       →  mark_running → ConversionService.run() → writes artifacts
                    →  mark_done(manifest)   |   mark_failed(type, message)

GET /v1/documents/<id>  →  status + manifest with a URL for each artifact
```

## On-disk layout

```
/data/jobs.sqlite3
/data/jobs/<uuid>/input/<original-name>
/data/jobs/<uuid>/output/document.md
                        /document.json
                        /tables/table-000.csv
                        /tables/table-000.html
                        /images/picture-000.png
```

The input file is retained — it is cheap and invaluable when debugging a bad conversion.
`DELETE /v1/documents/<id>` removes the whole directory and the row.

## API

Every route except `/health` and the documentation routes requires an `X-API-Key`
header, compared with `secrets.compare_digest` against the comma-separated `API_KEYS`
env var. A list of keys allows rotation without downtime.

All errors use one envelope:

```json
{ "error": { "code": "queue_full", "message": "Conversion queue is at capacity." } }
```

### `POST /v1/documents`

`multipart/form-data`:

- `file` — required, the document.
- `options` — optional JSON string:

```json
{ "ocr": true, "ocr_languages": ["en", "de"],
  "table_mode": "accurate", "image_scale": 2.0 }
```

Defaults come from env. `image_scale` is clamped to 1.0–4.0, `table_mode` is an enum
(`fast` / `accurate`), `ocr_languages` is a non-empty list of strings. Unknown fields are
**rejected**, not ignored — a silently dropped option typo is miserable to debug.

**202** with:

```json
{ "id": "…", "status": "queued", "filename": "report.pdf",
  "created_at": "…", "links": { "self": "/v1/documents/…" } }
```

**503** with `Retry-After` when the queue is full. **400** on invalid options. **401** on
a bad key.

### `GET /v1/documents/{id}`

The everything route. Returns **200** whenever the id exists, whatever the job state;
**404** otherwise.

```json
{ "id": "…", "status": "queued|running|succeeded|failed",
  "filename": "report.pdf",
  "created_at": "…", "started_at": "…", "finished_at": "…",
  "options": { "ocr": true, "ocr_languages": ["en"],
               "table_mode": "accurate", "image_scale": 2.0 },
  "error": null,
  "result": {
    "page_count": 12,
    "markdown": { "url": "/v1/documents/…/markdown", "bytes": 4821 },
    "document": { "url": "/v1/documents/…/document.json", "bytes": 91234 },
    "tables": [ { "index": 0, "rows": 8, "cols": 4, "page": 3,
                  "csv_url": "/v1/documents/…/tables/0?format=csv",
                  "html_url": "/v1/documents/…/tables/0?format=html" } ],
    "images": [ { "name": "picture-000.png", "kind": "picture", "page": 5,
                  "url": "/v1/documents/…/images/picture-000.png",
                  "bytes": 44210 } ]
  } }
```

`result` is `null` until the job succeeds. `error` is `{type, message}` when it fails and
`null` otherwise. Polling this one route tells a client everything, and every artifact is
reachable from the manifest without URL guessing.

### Artifact routes

Each returns **409** (not 404) while the job is `queued` or `running`, so a client can
distinguish "not yet" from "never existed", and **409** when the job failed.

| Route | Response |
|---|---|
| `GET /v1/documents/{id}/markdown` | `text/markdown` |
| `GET /v1/documents/{id}/document.json` | `application/json`, streamed from disk |
| `GET /v1/documents/{id}/tables/{index}?format=csv\|html` | `text/csv` or `text/html`, default `csv` |
| `GET /v1/documents/{id}/images/{name}` | `image/png` |

`name` and `index` are resolved through the manifest, never joined into a filesystem
path directly.

### `DELETE /v1/documents/{id}`

**204.** Removes the job directory and the row. **404** if unknown.

### `GET /health`

Unauthenticated. `{ "status": "ok", "models_loaded": true, "workers": 2,
"queue_depth": 3 }`. Used by the container healthcheck and load-balancer probes.

## Documentation

Swagger UI at `/docs`, ReDoc at `/redoc`, schema at `/openapi.json` — all behind a
`DOCS_ENABLED` env flag defaulting to true, and themselves unauthenticated (a schema is
not data).

Four things are done deliberately, because each is invisible by default and each breaks
the page for an integrator:

1. **A declared `APIKeyHeader` security scheme**, so Swagger UI shows the *Authorize*
   button and sends `X-API-Key` on try-it-out calls. Without it, every request from the
   docs page returns 401.
2. **Response models on every route**, including the error envelope, documented per
   status code. The 409 and 503 cases are exactly what a client needs and are undeclared
   by default.
3. **A worked example for the upload.** The `options` JSON-in-multipart field renders as
   a bare text box; it gets a description and a filled-in example.
4. **Tags, summaries, and descriptions** per route, plus an app-level description of the
   async flow: POST returns 202 with an id, poll `GET /v1/documents/{id}` until the
   status is terminal, then fetch artifacts.

A test asserts that `/openapi.json` is valid and that the security scheme is present.

## Conversion internals

### Converter lifecycle

Each worker thread owns a `DocumentConverter`. docling caches a pipeline internally per
distinct option set, so an unbounded option space would slowly leak pipelines. Two
bounds: `image_scale` is quantized to 0.5 steps before becoming part of the cache key,
and a thread's converter is rebuilt if it accumulates more than 8 cached pipelines. The
remaining option space is small and enumerable.

### Artifact extraction

Inside `ConversionService`, writing to `output/` and returning the manifest:

- the document exported to markdown → `document.md`
- the document serialized to JSON → `document.json`
- per table: dataframe → `table-NNN.csv`, plus `table-NNN.html`
- per picture: the generated image → `picture-NNN.png`, which requires
  `generate_picture_images=True` and `images_scale` set on the pipeline options

### Known risk: docling API drift

docling's export API names have churned across releases. Pin an exact version in
`requirements.txt` and **verify every docling call against that pinned version during
implementation** rather than trusting prior knowledge of the API. This applies to the
export methods, the pipeline option names, and the picture-image accessor.

## Failure handling

| Failure | Handling |
|---|---|
| Runaway conversion | docling's own `document_timeout` pipeline option (`JOB_TIMEOUT_SECONDS`, default 600). A worker thread cannot be safely aborted from outside, so the timeout must be cooperative and in-process. |
| Conversion error or failure status | Job marked `failed` with `{type, message}` drawn from the docling result. |
| Unexpected exception | Caught, logged with job id and traceback, job marked `failed` with type `internal_error`. A worker thread never dies from a bad job. |
| Disk write failure | Same as above; job marked `failed`. |
| Worker thread dies anyway | The pool supervisor restarts it. |
| Container died mid-job | Startup sweep marks `running` jobs `failed` with type `interrupted`; `queued` jobs are re-enqueued and simply run. |

The crash-recovery sweep is what makes the SQLite choice pay off: a restart loses only
in-flight work, and says so explicitly instead of leaving rows stuck in `running`
forever.

## Testing

Test-driven: tests precede implementation.

- **`JobStore`** against a temp SQLite — status transitions, delete removes both row and
  directory, the startup recovery sweep.
- **Options schema** — unknown fields rejected, `image_scale` clamped, `table_mode` enum
  enforced.
- **Artifact writer** against a fixture DoclingDocument, no real conversion — manifest
  shape, filenames, and that `images/{name}` cannot escape the job directory.
- **Full lifecycle** through `TestClient` with a fake `ConversionService` that writes
  canned artifacts: POST → poll → fetch each artifact, plus the 401 / 404 / 409 / 503
  paths. Runs in seconds with no models loaded.
- **OpenAPI** — schema is valid and declares the security scheme.
- **One opt-in slow test** (`@pytest.mark.slow`) converting a one-page fixture PDF
  through real docling, proving the wiring end to end without paying for it on every
  commit.

## Container

Multi-stage build:

- **Builder** — installs CPU-only torch wheels (several GB smaller than the CUDA
  default) and downloads the docling model weights into `/opt/models`.
- **Runtime** — copies the virtualenv and weights, runs as a non-root user that owns
  `/data`, and sets the offline env flags so nothing reaches for HuggingFace at runtime.

`VOLUME /data`, a `HEALTHCHECK` against `/health`, and a `docker-compose.yml` wiring the
volume and env.

**Uvicorn runs with exactly one worker.** The queue and thread pool live in the process;
multiple uvicorn workers would mean multiple independent queues contending over one
SQLite file. Scale with `WORKER_THREADS`, not processes.

### Configuration

| Env var | Default | Meaning |
|---|---|---|
| `API_KEYS` | *(required)* | Comma-separated valid keys |
| `DATA_DIR` | `/data` | Volume root |
| `WORKER_THREADS` | `2` | Conversion threads |
| `QUEUE_MAX` | `32` | Queue capacity before 503 |
| `JOB_TIMEOUT_SECONDS` | `600` | docling `document_timeout` |
| `DEFAULT_OCR` | `true` | Default option |
| `DEFAULT_OCR_LANGUAGES` | `en` | Default option |
| `DEFAULT_TABLE_MODE` | `accurate` | Default option |
| `DEFAULT_IMAGE_SCALE` | `2.0` | Default option |
| `DOCS_ENABLED` | `true` | Serve `/docs`, `/redoc`, `/openapi.json` |
| `LOG_LEVEL` | `INFO` | Logging |

Logs are structured, carrying job id, duration, and page count for completed
conversions.

## Explicitly out of scope (backlog)

Considered and deliberately deferred:

- **`GET /v1/documents` list route** — paginated listing of jobs. The natural companion
  to manual reclamation; cut from v1 by decision.
- **TTL cleanup sweeper** — without it the volume grows unbounded. Manual reclamation via
  `DELETE` is the v1 answer.
- **Upload size limits and MIME validation** — one very large upload can occupy a worker
  for a long time.
- **Webhook callback on completion** — polling already works.
- **Full-page image renders** — a much larger output than picture crops.
- **`ProcessPoolExecutor` workers** — true parallelism at roughly 1.5–2 GB of weights per
  process. Adopt only if threads measurably bottleneck.
- **S3/MinIO artifact storage** — needed only for stateless multi-replica deploys.

## Refinements made during planning

Verifying the docling 2.129.0 API surfaced six points where this design needed
adjusting. They are listed with their rationale in the "Refinements to the spec"
section of `docs/superpowers/plans/2026-09-19-docling-extraction-api.md`, and that
plan is authoritative where the two differ. In brief:

1. `queue.Queue`, not `asyncio.Queue` — the consumers are OS threads.
2. The per-thread converter cache is a bounded LRU of `CONVERTER_CACHE_SIZE`
   (default 4), replacing the "rebuild past 8 pipelines" rule.
3. Non-timeout `PARTIAL_SUCCESS` counts as success, with docling's messages
   recorded in the manifest under `warnings`.
4. The OCR engine is pinned to EasyOCR rather than docling's `OcrAutoOptions()`.
5. Worker supervision is structural — the loop body cannot let an exception
   escape — rather than a separate monitor thread.
6. Requirements are split three ways so the fast test suite installs without torch.
