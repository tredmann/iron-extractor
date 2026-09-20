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

**Exactly one uvicorn worker.** The queue and thread pool live in the process;
a second uvicorn worker means a second queue contending over one SQLite file.
Scale with `WORKER_THREADS`.

**Every non-2xx response uses `{"error": {"code": ..., "message": ...}}`.**
Validation failures are rewritten to 400 by the `RequestValidationError`
handler, so `app/main.py` strips FastAPI's auto-generated 422 from the schema —
otherwise the docs advertise a response the server never sends.

**The manifest is a cross-module contract.** `artifacts.write_artifacts`
produces `page_count`/`warnings`/`markdown.bytes`/`document.bytes`/
`tables[].index,rows,cols,page,csv,html`/`images[].name,kind,page,bytes`, and
`schemas._build_result` reads exactly those. It stores **filenames**; the HTTP
layer builds URLs. Changing a key breaks three modules.

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
there is no API to enumerate what is on the volume. OCR weights are
English-only in the image. Logs are plain text, not structured.

## Design documents

- Spec: `docs/superpowers/specs/2026-09-19-docling-api-design.md`
- Plan: `docs/superpowers/plans/2026-09-19-docling-extraction-api.md` (records
  six refinements made after verifying docling's actual API)
