# CONTEXT.md — the project's domain language

One word per concept, one concept per word. This file exists because three
words in this codebase mean more than one thing, and a fourth synonym is
always one commit away.

**It describes what the code says today.** Nothing here mandates a rename; the
three known collisions are flagged where they live and indexed below, so a
future rename campaign is a costed decision rather than an archaeology
project.

**It governs new code.** Where a concept already has two words, an entry names
which one new code uses. That freezes each collision at today's size instead
of letting it widen.

**Keeping it true:** a new concept gets an entry in the commit that introduces
it. Reusing a word already defined here needs nothing.

Docling's own vocabulary — `self_ref`, `bbox`, `kind`, provenance,
recognizer — is deliberately out of scope. It is defined in CLAUDE.md under
"Integrating this service", beside the caveats that make it matter.

## Known collisions

Three, all frozen rather than fixed:

- **`document`** — the route noun `/v1/documents/{job_id}` names a job, not a
  document. See *document*, *job*.
- **`picture`** — leaks past the docling boundary in the filename
  `picture-000.png` and the value `kind: "picture"`, both caller-visible. See
  *image*.
- **`worker`** — a conversion thread here, a uvicorn process in the Dockerfile
  and in `--workers 1`. See *worker*.

## Terms

### artifact

Anything a conversion writes to disk for a caller to fetch later: the
Markdown, the DoclingDocument JSON, each table as CSV and HTML, each image as
PNG, each page as Markdown and text. Artifacts live under a job's `output/`
directory and are listed in its manifest.

*Where:* `app/conversion/artifacts.py` writes them; `JobPaths` resolves them;
the artifact routes serve them.

### conversion

One run of a document through docling — the work a job exists to perform.
Begins when a worker picks the job up, ends when the manifest is written.

*Not to be confused with:* **job**, which is the record and the unit of
queueing. A job that never runs still exists; a conversion is the running.

*Where:* `ConversionService.run`; `app/conversion/`.

### document

**The file the caller uploaded.** Never anything else — not the job, not
docling's parsed model.

*Not to be confused with:* **DoclingDocument** (docling's parsed model) and
**job** (the unit of work). All three were called "document" before this file
existed, which is what it was written to stop.

*Collision:* `/v1/documents/{job_id}` names a job. The route is not changing;
read `documents` there as "documents you have submitted" and note the path
parameter says `job_id` for a reason.

### DoclingDocument

Docling's parsed representation of a converted document — its full structured
model, with texts, tables, pictures and page provenance. **Always written in
full**, never shortened to "document".

*Where:* produced by `docling`; served verbatim as `document.json`;
`app/conversion/artifacts.py` is duck-typed against it and imports nothing
from docling.

### image

**Our word for an extracted picture, and the one every caller sees.** Used for
the artifact, the route, the manifest array and the response field.

*Boundary rule:* `picture` is *docling's* word and appears only where we touch
docling's API — `document.pictures`, `PictureItem`, and `_write_pictures`,
which iterates them. Everywhere else, including every name a caller sees, the
word is `image`.

*Collision:* two of docling's picture-words cross the boundary into
caller-visible output — the filename `picture-000.png`, and `kind: "picture"`
(which can also be `chart` or another docling label). Both are frozen: they
are part of the manifest contract.

### job

**The unit of work and its record**: one upload, its options, its status, its
timings, and its manifest once it succeeds. Identified by a UUID. The dominant
noun in this codebase and the one to reach for by default.

*Where:* `JobStore`, `Job`, `JobPaths`, `WorkerPool`, and the `{job_id}` path
parameter on every route.

### manifest

The dict `write_artifacts` returns and the store persists: what the conversion
produced, recording artifact **filenames**, never URLs and never content.

*Not to be confused with:* **result**, which is the same data rendered for
HTTP. The manifest is the stored form; the result is the served form. CLAUDE.md
holds the key-by-key contract.

### options

The four per-request knobs a caller may send, and only these: `ocr`,
`ocr_languages`, `table_mode`, `image_scale`. Unknown keys are rejected with
400.

*Not to be confused with:* **settings** — the operator's environment
configuration (`WORKER_THREADS`, `MODELS_DIR`…), which a caller cannot touch.
Settings supply the defaults that options override.

### page

One page of the converted document, numbered as docling numbers it. Carries
its own Markdown and text, and owns the images and tables whose provenance
places them there.

*Note:* pages do not always sum to the document — content docling could not
place appears in the whole-document Markdown and on no page. There is no
pagination anywhere in this API, so `page` never means a page of results.

### queue

The bounded buffer between accepting a job and running it, drained by workers.
Capacity is `QUEUE_MAX` (default 32); a full queue is backpressure, answered
with **503 `queue_full`** rather than an unbounded backlog.

### result

The manifest rendered for HTTP: filenames become URLs, and the shape becomes
`JobResult`. `null` until a job succeeds.

*Not to be confused with:* **manifest**, the stored form. One is the other
after `build_*_refs` has run.

### status

A job's lifecycle, a closed set of four: `queued` → `running` → `succeeded` or
`failed`. The arrows only go one way — nothing ever returns to `queued`. A
restart does not rewind a job either: `recover_interrupted()` fails whatever
was `running` and re-enqueues rows that were still `queued`, which never
changed status.

*Not to be confused with:* docling's own conversion status (`success`,
`partial_success`), which `ConversionService` consumes and never exposes.

### table

A table docling extracted, written as both CSV and HTML and identified by its
index within the document.

*Note:* a table TableFormer detected but could not resolve still appears, with
`rows: 0, cols: 0`.

### warning

A non-fatal problem recorded against a successful conversion. Two sources:
messages docling raised during the conversion, and per-item export failures
(one table, picture or page that could not be written). A warning never fails
a job — that is the whole point of it.

*Note:* absolute paths in docling's messages are reduced to filenames before
they travel, because warnings are served over HTTP.

### worker

**A conversion thread.** `WORKER_THREADS` sets how many; they drain the queue
and run conversions. Bare "worker" always means this.

*Collision:* uvicorn also calls its processes workers. That one is **always
written in full as "uvicorn worker"**, never bare — there is exactly one of
them, deliberately, because the queue and the threads live inside that single
process.
