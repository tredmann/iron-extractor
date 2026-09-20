# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Setup (Python 3.12 — torch wheels are unreliable on 3.13+)
/opt/homebrew/opt/python@3.12/bin/python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # no docling, no torch (~2GB saved)

.venv/bin/pytest                                # fast suite: no models, seconds
.venv/bin/pytest tests/test_pool.py -v          # one file
.venv/bin/pytest tests/test_pool.py::test_name -v
.venv/bin/ruff check app/ tests/                # must stay clean
```

`pytest.ini` sets `addopts = -m "not slow"`, so a plain run **deselects** the
docling-dependent tests rather than skipping them — expect a "3 deselected"
line. Clear the filter with `-m ""` to run them, which only works where real
docling is installed (i.e. in the container).

```bash
# Container. Requires ~5GB disk; a cold build takes ~10 minutes.
docker compose build
API_KEYS=<key> docker compose up -d

# The docling-dependent tests, against real weights
docker compose run --rm -e API_KEYS=test test pytest -m "" \
  tests/test_converters.py tests/test_end_to_end.py -v
```

`docker build .` with no `--target` builds the **test** stage, because it is
last in the Dockerfile. Use `--target runtime` for the deployable image.
Compose already names the stage per service.

## Architecture

An async document-conversion API over [docling](https://github.com/docling-project/docling).
`POST` a document → 202 with a UUID → poll → fetch artifacts from separate
routes. One process: a bounded queue drained by worker **threads**, SQLite for
job state, artifacts on a mounted volume.

Request lifecycle, worth tracing once before changing anything:

```
POST /v1/documents → parse_options → paths.create → run_in_threadpool(_write_upload)
                   → store.create(queued) → pool.enqueue → 202
worker thread      → store.mark_running → ConversionService.run
                   → ConverterProvider.get(options) → docling → write_artifacts
                   → manifest dict → store.mark_succeeded
GET  /v1/documents/{id} → build_job_detail → manifest filenames become URLs
```

Four layers that do not know about each other: `JobStore` (persistence),
`ConversionService` (docling orchestration), `WorkerPool` (execution), and the
API layer. `app/main.py` is wiring only.

## Invariants — breaking these has caused real bugs here

**docling is imported only inside methods of `app/conversion/converters.py`.**
No module-level `import docling` anywhere in `app/`. This is what lets the fast
suite run without torch, and it means a docling upgrade touches one file.
`app/conversion/artifacts.py` is duck-typed against `DoclingDocument` and
imports nothing from docling at all.

**Resolve the job from the store before constructing any filesystem path.**
`JobPaths` deliberately does not validate `job_id`. A `DELETE` route that
skipped the store lookup once wiped the entire data directory via
`%2e%2e`. `JobPaths.remove` now also raises `ValueError` when the resolved
parent is not `jobs_root` — keep both layers.

**Artifact routes resolve names from the manifest before touching disk.**
`tables/{index}` and `images/{name}` look the entry up in `job.manifest` and
404 first; `JobPaths.image_file`'s `ValueError` is the second line of defence,
not the first.

**Lock ordering is `_in_flight_lock` → queue lock, never the reverse.**
`WorkerPool.enqueue` clears `_idle` and puts the item inside one lock hold, so
no item is ever visible in the queue while `wait_idle` reads `True`. Reversing
the order deadlocks shutdown. `_update_idle_locked` is the single definition of
"idle" — call it, don't reimplement the condition.

**A worker thread must never die.** The `try/except Exception` around
`_process` in `WorkerPool._run` *is* the supervision mechanism; there is no
monitor thread.

**OCR language support is decided at the 400 boundary, not in a worker.**
EasyOCR picks its recognizer from the whole language *set*: `["en"]`
exactly gets `english_g2.pth`, any other Latin set gets `latin_g2.pth`.
With `MODELS_DIR` set, docling forces `download_enabled=False`, so a
recognizer the image lacks surfaces as a bare `FileNotFoundError` deep
inside `_process` — after the caller already has a 202. `create_document`
calls `app.state.check_ocr_support` *before* `paths.create`, and
`ConverterProvider._resolve_recognizer` ports EasyOCR's dispatch ladder
while reading every language list and filename back out of
`easyocr.config`, so an upgrade can shift the dispatch but never leave
stale data. `tests/test_converters.py::test_resolver_agrees_with_easyocr`
is the drift guard. The check is skipped when `ocr` is false, because
`EasyOcrModel` builds no Reader then.

**`job.error_message` is served over HTTP.** `WorkerPool._process` logs an
unexpected exception in full and records only its type — docling failures
routinely embed container-internal paths under `MODELS_DIR`.

**The `/pages` routes are the only handlers that read artifact bytes
themselves.** Every other artifact route hands Starlette a `FileResponse`,
which it serves off the loop. `/pages` inlines content, so it reads the files
directly -- and on a long document that is many reads. They go through
`run_in_threadpool` for that reason; doing them inline would stall every other
request in this single-process server. `_read_page_text` returns `None` for an
unreadable file rather than raising, so one missing file cannot sink a whole
`/pages` response.

**Exactly one uvicorn worker.** The queue and thread pool live in the process;
a second uvicorn worker means a second queue contending over one SQLite file.
Scale with `WORKER_THREADS`.

**Every non-2xx response uses `{"error": {"code": ..., "message": ...}}`.**
Validation failures are rewritten to 400 by the `RequestValidationError`
handler, so `app/main.py` strips FastAPI's auto-generated 422 from the schema —
otherwise the docs advertise a response the server never sends.

**The manifest is a cross-module contract.** `artifacts.write_artifacts`
produces `page_count`/`warnings`/`markdown.bytes`/`document.bytes`/
`tables[].index,rows,cols,page,csv,html,caption,self_ref,bbox`/
`images[].index,name,kind,page,bytes,width,height,caption,self_ref,bbox`/
`pages[].page,width,height,markdown,text`, and the HTTP layer reads exactly
those. It stores **filenames**; the HTTP layer builds URLs. Changing a key
breaks three modules.

Two of those filename fields are written but never read back:
`tables[].csv`/`tables[].html` and `pages[].markdown`/`pages[].text`.
`build_table_refs` builds `csv_url`/`html_url` from `entry["index"]` alone,
and `_read_page_contents` builds each page's file path from
`paths.page_file(job_id, page_no, ...)` alone — neither looks at the
filename the manifest recorded. They exist for readability and as a
paper trail of what `write_artifacts` produced, not because anything
depends on them. A change to `JobPaths`'s naming scheme would desynchronise
them from the files actually on disk with nothing to catch it.

`schemas.build_image_refs` / `build_table_refs` are the *only* place the HTTP
side reads those arrays, and `build_page_details` is the only place it reads
`pages[]`. `_build_result` and the `/images` and `/tables` listing routes all
call the former pair, which is what keeps the job detail and the listings from
drifting; `tests/test_routes_listings.py::test_listing_matches_the_job_detail_exactly`
is the guard. All three read with `.get()` so a manifest written before a
field existed still renders, as null.

## Testing approach

The fast suite exercises the full HTTP stack with **no model and no torch**:
`create_app(settings=..., service=...)` takes an injectable conversion service,
and `tests/conftest.py`'s `recording_service` fixture substitutes one. That
injection is why 190+ tests run in about a second. Its `hold`/`release` gate is
how you test a job observably stuck in `running` without sleeping.

Concurrency tests must be deterministic — `threading.Event`/`Barrier`, never
`sleep`. A racy test here passed 15 consecutive runs while carrying a real bug;
reason structurally rather than trusting a green run.

## Known gaps (deliberate, documented in README)

No list route and no TTL sweeper — reclamation is manual via `DELETE`, and
there is no API to enumerate what is on the volume. OCR weights cover the
Latin script only (`english_g2` + `latin_g2`); other scripts are rejected
with 400 at request time rather than shipped. Logs are plain text, not
structured.

## Design documents

- Spec: `docs/superpowers/specs/2026-09-19-docling-api-design.md`
- Plan: `docs/superpowers/plans/2026-09-19-docling-extraction-api.md` (records
  six refinements made after verifying docling's actual API)
