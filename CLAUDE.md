# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository, and for
any agent integrating this service from another codebase. Read **Architecture**
and **Invariants** before changing code; read **The HTTP contract** and
**Integrating this service** before calling it from elsewhere.

## How to work here

**YAGNI.** Build what was asked and nothing beside it. No speculative
parameters, no abstraction for a second caller that does not exist, no "while
I'm here" extras. When you believe something extra genuinely belongs in the
change, say what and why, then **wait for an explicit yes** before building it.
Scaling the work up is the user's call, not yours. (Scaling it *down* is the
same rule in reverse: if part of the ask looks wrong, finish it anyway under a
stated assumption and flag the concern.)

**Reach for what already exists, in this order:** the Python standard library,
then a dependency this project already has, then a new dependency, then your
own implementation. Hand-rolling something the stdlib does is the failure this
rule exists to prevent — but so is adding a package to avoid ten lines.

A new dependency is itself a YAGNI decision and needs the same explicit yes.
Every requirement here is exact-pinned (`==`), the runtime image is already
~5GB because of torch, and the container runs fully offline — so a dependency
is a permanent, weighed choice, not a convenience. Say what you need, what in
`requirements-base.txt` you checked first, and why it did not fit.

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
docling-dependent tests rather than skipping them — expect a "deselected" line
whose count grows as slow tests are added. Clear the filter with `-m ""` to run
them, which only works where real docling is installed (i.e. in the container).
The `slow` tests live in `tests/test_converters.py` and
`tests/test_end_to_end.py`. One skip in the fast suite is expected: docling is
absent from the dev venv by design.

```bash
# Container. Requires ~5GB disk; a cold build takes ~10 minutes.
docker compose build
API_KEYS=<key> docker compose up -d

# Everything, including the tests that need real weights
docker compose run --rm -e API_KEYS=test test pytest -m "" -q
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
POST /v1/documents → parse_options → check_ocr_support (threadpool)
                   → paths.create → run_in_threadpool(_write_upload)
                   → store.create(queued) → pool.enqueue → 202
worker thread      → store.mark_running → ConversionService.run
                   → ConverterProvider.get(options) → docling → write_artifacts
                   → manifest dict → store.mark_succeeded
GET  /v1/documents/{id} → build_job_detail → manifest filenames become URLs
```

Four layers that do not know about each other: `JobStore` (persistence),
`ConversionService` (docling orchestration), `WorkerPool` (execution), and the
API layer. `app/main.py` is wiring only.

## The HTTP contract

Every route except `/health` requires an `X-API-Key` header. Every non-2xx
response is `{"error": {"code": ..., "message": ...}}`.

| Route | Answers | Notes |
|---|---|---|
| `GET /health` | 200 | Unauthenticated. The container healthcheck |
| `POST /v1/documents` | 202 400 401 413 503 | Multipart: `file`, optional `options` JSON |
| `GET /v1/documents/{id}` | 200 401 404 | Status, and every artifact link once succeeded |
| `DELETE /v1/documents/{id}` | 204 401 404 | Removes the job row and its whole directory |
| `GET …/markdown` | 200 401 404 409 | Whole document, `text/markdown` |
| `GET …/document.json` | 200 401 404 409 | The full `DoclingDocument` |
| `GET …/tables` | 200 401 404 409 | `{"tables": [...]}` |
| `GET …/tables/{index}` | 200 400 401 404 409 | `?format=csv` (default) or `html` |
| `GET …/images` | 200 401 404 409 | `{"images": [...]}` |
| `GET …/images/{name}` | 200 400 401 404 409 | Always PNG |
| `GET …/pages` | 200 401 404 409 | Every page, content inline |
| `GET …/pages/{page_no}` | 200 400 401 404 409 | One page, content inline |

**409 means "not ready", 404 means "not there".** Artifact routes answer 409
while a job is `queued` or `running`, and 409 for a `failed` job — which has no
artifacts and never will. Distinguishing the two is the point; do not collapse
them into one retry path.

`options` accepts exactly four keys and rejects unknown ones with 400:
`ocr` (bool), `ocr_languages` (list), `table_mode` (`fast`/`accurate`),
`image_scale` (1.0–4.0, snapped to 0.5 steps).

## Integrating this service

The flow is: `POST` → poll `GET /v1/documents/{id}` until `status` is
`succeeded` or `failed` → read `result` → `DELETE` when done. `result` is
`null` until the job succeeds, and carries links to every artifact once it has.

Reclamation is the caller's job. There is no TTL sweeper and no list route, so
a client that forgets to `DELETE` leaks disk with no API to discover what it
left behind.

### Caveats that bite callers

These are behaviours, not bugs. Most are pinned by a test; the two marked
*observed* are field notes from real documents and are not reproduced by any
fixture here.

**OCR languages are chosen as a set, and the image ships Latin only.** EasyOCR
picks one recognizer for the whole `ocr_languages` list: `["en"]` *exactly*
uses `english_g2.pth`; every other Latin set (`["de"]`, `["en","de"]`,
`["fr"]`) uses `latin_g2.pth`. Both are baked in. Non-Latin scripts (Cyrillic,
CJK, Arabic, Devanagari, Thai) are **not**, and the container runs offline, so
they are refused up front with **400 `unsupported_ocr_language`** naming the
missing weight file. The same 400 covers codes EasyOCR does not know and
combinations it refuses — its non-Latin models pair only with English, so
`["ja","de"]` is impossible regardless of what is installed. Requests with
`"ocr": false` are never refused on language grounds.

**Pages do not always sum to the document.** Content docling could not place on
a page appears in `/markdown` but in no page's export. Usually nothing is
unplaced (*observed*: a 12-page deck measured 142 document lines against 142
across its pages) — but when you need guaranteed-complete text, read
`/markdown` or `document.json`, not a concatenation of `/pages`. The mechanism
is pinned by
`tests/test_artifacts.py::test_content_without_provenance_lands_on_no_page`;
how often it bites is not.

**`GET /pages` is unbounded.** Response size grows with the document and no
parameter reduces it. `/pages/{page_no}` is the bounded alternative.

**`null` and `""` differ.** On a page, `markdown`/`text` are `null` only when
the file could not be read; `""` is a page docling registered but extracted
nothing from. A caption is `null`, never `""`, when an item has none.

**Image `index` has gaps.** It is docling's picture index, not the array
position — pictures carrying no image data are omitted entirely. It always
agrees with `self_ref`, which is what indexes into `document.json`.

**`bbox` carries `coord_origin`.** docling reports PDF provenance
`BOTTOMLEFT` and other sources `TOPLEFT`, so `t` and `b` are meaningless
without it. The `l`/`t`/`r`/`b` names are docling's, kept so cross-referencing
into `document.json` needs no translation. `bbox` is `null` for an item with no
provenance.

**Extraction is faithful, not clean.** Two consequences seen on real files: a
PDF using an icon font yields Unicode private-use codepoints (`U+E000`–`U+F8FF`)
in `markdown` and `text`, because no standard character exists to map them to;
and a table TableFormer cannot resolve comes back with `rows: 0, cols: 0` while
still appearing in the manifest (*observed* once, on a synthetic PDF; `rows`
and `cols` are docling's `num_rows`/`num_cols` passed through unclamped, so the
mechanism is real even though nothing here reproduces it). Strip `U+E000`–`U+F8FF` if you feed the text
downstream, and treat a zero-dimension table as "detected but not parsed".

**Failure detail depends on which failure it was.** `error.message` names the
exception type only for `error_type: internal_error` — an unexpected crash,
where the text would be a Python exception embedding a container path. For
`timeout` and `conversion_failed`, the outcomes a real docling run actually
produces, it carries docling's own error text. `result.warnings` likewise mixes
two shapes: entries docling raised during conversion come through verbatim,
while a per-item export failure (one table, picture or page) reads as the item
plus an exception type. The full exception is always in the server logs.

**`models_loaded` is not readiness.** `/health` reports whether any converter
has been built yet, which only happens on the first conversion. It is `false`
on a healthy idle server; the healthcheck only requires a 200.

**The first request after a restart is slow.** Model weights load lazily in a
worker thread on the first conversion. Size client timeouts for that, not for
the steady state.

## Invariants — breaking these has caused real bugs here

**docling is imported only inside methods of `app/conversion/converters.py`.**
No module-level `import docling` anywhere in `app/`. This is what lets the fast
suite run without torch, and it means a docling upgrade touches one file.
`app/conversion/artifacts.py` is duck-typed against `DoclingDocument` and
imports nothing from docling at all.

**Importing easyocr is expensive and belongs off the event loop.** It executes
easyocr's `__init__`, which pulls in torch, torchvision and cv2. `unsupported_ocr_reason`
reaches that import, so `create_document` calls it through `run_in_threadpool`.
Called inline it froze the whole server for seconds on the first `POST` after
every restart — one process, one event loop, every other request blocked behind
it.

**Resolve the job from the store before constructing any filesystem path.**
`JobPaths` deliberately does not validate `job_id`. A `DELETE` route that
skipped the store lookup once wiped the entire data directory via
`%2e%2e`. `JobPaths.remove` now also raises `ValueError` when the resolved
parent is not `jobs_root` — keep both layers.

**Artifact routes resolve names from the manifest before touching disk.**
`tables/{index}`, `images/{name}` and `pages/{page_no}` look the entry up in
`job.manifest` and 404 first; `JobPaths.image_file`'s `ValueError` is the second
line of defence, not the first. For pages the check is load-bearing for a
second reason: docling's exporter returns `""` for an out-of-range page rather
than raising, so page 99 of a two-page document would read as blank instead of
missing.

**Lock ordering is `_in_flight_lock` → queue lock, never the reverse.**
`WorkerPool.enqueue` clears `_idle` and puts the item inside one lock hold, so
no item is ever visible in the queue while `wait_idle` reads `True`. Reversing
the order deadlocks shutdown. `_update_idle_locked` is the single definition of
"idle" — call it, don't reimplement the condition.

**A worker thread must never die.** The `try/except Exception` around
`_process` in `WorkerPool._run` *is* the supervision mechanism; there is no
monitor thread.

**OCR language support is decided at the 400 boundary, not in a worker.**
With `MODELS_DIR` set, docling forces `download_enabled=False`, so a recognizer
the image lacks surfaces as a bare `FileNotFoundError` deep inside `_process` —
after the caller already has a 202. `create_document` calls
`app.state.check_ocr_support` *before* `paths.create`.
`ConverterProvider._resolve_recognizer` ports EasyOCR's dispatch ladder while
reading every language list and filename back out of `easyocr.config`, so an
upgrade can shift the dispatch but never leave stale data.
`tests/test_converters.py::test_resolver_agrees_with_easyocr` is the drift
guard. The check is skipped when `ocr` is false, because `EasyOcrModel` builds
no Reader then.

**Our own exception text never reaches HTTP; docling's own error strings do.**
Two paths, and the difference is deliberate. `WorkerPool._process`'s
`except Exception` branch and the three per-item writers in `artifacts.py` log
the exception in full and record only its type — a Python exception raised
*here* embeds a container path (`MODELS_DIR`, or the job's own directory in an
`OSError` from a full volume). But `ConversionFailed` carries `exc.message`
through verbatim, and `ConversionService.run` builds that from docling's own
`error_message` strings; the same strings also pass through as the leading
entries of `manifest["warnings"]`. That is what makes a `timeout` or
`conversion_failed` diagnosable at all. If docling is ever found to embed paths
in those strings, scrub at the `ConversionService` boundary — do not flatten
`ConversionFailed` to a type name, which would make every real failure opaque.
`tests/test_pool.py::test_internal_error_message_does_not_leak_exception_detail`
pins the first path; `test_pool.py`'s `timeout`/`"too slow"` assertions pin the
second.

**The `/pages` routes are the only handlers that read artifact bytes
themselves.** Every other artifact route hands Starlette a `FileResponse`,
which it serves off the loop. `/pages` inlines content, so it reads the files
directly — and on a long document that is many reads. They go through
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
side maps those arrays into responses, and `build_page_details` is the only
place it maps `pages[]`. `_build_result`, the `/images` and `/tables` listing
routes and the `/pages` routes all go through them, which is what keeps the job
detail and the listings from drifting;
`tests/test_routes_listings.py::test_listing_matches_the_job_detail_exactly`
and `tests/test_routes_pages.py::test_page_entries_are_identical_to_the_listing_routes`
are the guards. All three read with `.get()` so a manifest written before a
field existed still renders, as null. (The routes additionally read
`manifest["pages"]` and the other arrays directly for their 404 membership
checks — that is the existence test, not the mapping.)

## Testing approach

The fast suite exercises the full HTTP stack with **no model and no torch**:
`create_app(settings=..., service=...)` takes an injectable conversion service,
and `tests/conftest.py`'s `recording_service` fixture substitutes one. That
injection is why the whole suite runs in about a second. Its `hold`/`release`
gate is how you test a job observably stuck in `running` without sleeping.

`docling-core` *is* installed in the dev venv (via `requirements-base.txt`; it
needs no torch), so `tests/test_artifacts.py`
builds real `DoclingDocument` objects and exercises the real exporters in the
fast suite. Reach for that before reaching for the container.

Concurrency tests must be deterministic — `threading.Event`/`Barrier`, never
`sleep`. A racy test here passed 15 consecutive runs while carrying a real bug;
reason structurally rather than trusting a green run. A polling loop needs a
terminal assertion (`for … else: raise`): two tests once asserted 409 after
polling for `failed`, which a still-`running` job also answers, so they passed
without ever observing the state they named.

## Known gaps (deliberate, documented in README)

No list route and no TTL sweeper — reclamation is manual via `DELETE`, and
there is no API to enumerate what is on the volume. OCR weights cover the
Latin script only (`english_g2` + `latin_g2`); other scripts are rejected
with 400 at request time rather than shipped. Page renders are not produced
(`generate_page_images=False`), so `/pages` returns the pictures found on a
page, never an image of the page itself. Logs are plain text, not structured.

## Design documents

- Spec: `docs/superpowers/specs/2026-09-19-docling-api-design.md`
- Plan: `docs/superpowers/plans/2026-09-19-docling-extraction-api.md` (records
  six refinements made after verifying docling's actual API)
- Spec: `docs/superpowers/specs/2026-09-20-per-page-artifacts-design.md`
- Plan: `docs/superpowers/plans/2026-09-20-per-page-artifacts.md`
