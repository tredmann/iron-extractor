# Docling Extraction API

Convert documents with [docling](https://github.com/docling-project/docling) over HTTP.
Upload a document, get a UUID back, retrieve the results when the job finishes.

## Quick start

```bash
API_KEYS=dev-key docker compose up --build
```

Then open http://localhost:8000/docs and click **Authorize** with your key.

## Flow

```bash
# 1. Upload — returns 202 with a job id
curl -X POST http://localhost:8000/v1/documents \
  -H "X-API-Key: dev-key" \
  -F "file=@report.pdf" \
  -F 'options={"ocr": true, "ocr_languages": ["en"], "table_mode": "accurate", "image_scale": 2.0}'

# 2. Poll until status is succeeded or failed
curl http://localhost:8000/v1/documents/<id> -H "X-API-Key: dev-key"

# 3. List what the document produced (or read `result` from step 2 — same data)
curl http://localhost:8000/v1/documents/<id>/images -H "X-API-Key: dev-key"
curl http://localhost:8000/v1/documents/<id>/tables -H "X-API-Key: dev-key"
curl http://localhost:8000/v1/documents/<id>/pages   -H "X-API-Key: dev-key"
curl http://localhost:8000/v1/documents/<id>/pages/1 -H "X-API-Key: dev-key"

# 4. Fetch artifacts
curl http://localhost:8000/v1/documents/<id>/markdown       -H "X-API-Key: dev-key"
curl http://localhost:8000/v1/documents/<id>/document.json  -H "X-API-Key: dev-key"
curl "http://localhost:8000/v1/documents/<id>/tables/0?format=csv" -H "X-API-Key: dev-key"
curl http://localhost:8000/v1/documents/<id>/images/picture-000.png -H "X-API-Key: dev-key" -o picture.png

# 5. Reclaim the space
curl -X DELETE http://localhost:8000/v1/documents/<id> -H "X-API-Key: dev-key"
```

### Listing images and tables

`GET /v1/documents/{id}/images` and `GET /v1/documents/{id}/tables` return just
one array each, in exactly the shape `result.images` / `result.tables` use on
the job detail — so clients that only want artifacts need not parse the whole
job. Both read the manifest and touch no files, and a document with none of a
kind returns `{"images": []}` rather than a 404.

```json
{
  "images": [
    {
      "index": 0,
      "name": "picture-000.png",
      "kind": "chart",
      "page": 3,
      "width": 1240,
      "height": 780,
      "caption": "Fig. 2 — Revenue by region",
      "self_ref": "#/pictures/0",
      "bbox": { "l": 72.0, "t": 640.5, "r": 523.2, "b": 310.8,
                "coord_origin": "BOTTOMLEFT" },
      "url": "/v1/documents/<id>/images/picture-000.png",
      "bytes": 84213
    }
  ]
}
```

Tables carry `caption`, `self_ref` and `bbox` alongside the existing `index`,
`rows`, `cols`, `page`, `csv_url` and `html_url`.

Notes on the metadata:

- `self_ref` is docling's own node reference. It indexes straight into
  `document.json`, which is the point — pair a listing with that artifact and
  you can reach the item's full docling record without re-deriving it.
- `bbox` carries `coord_origin` because docling reports PDF provenance
  bottom-left and other sources top-left; without it `t` and `b` are
  ambiguous. The `l`/`t`/`r`/`b` names are docling's, kept deliberately so
  cross-referencing needs no translation. `bbox` is `null` for an item with
  no provenance.
- `caption` is `null`, never `""`, when an item has no caption.
- Image `index` is docling's picture index, not the array position: pictures
  carrying no image are omitted entirely, so the sequence can have gaps. It
  always agrees with `self_ref`.

Artifact routes return **409** while a job is queued or running, and **404** if the
job does not exist — so "not ready" is always distinguishable from "not found".

Markdown, `document.json`, CSV tables and images are served **inline**, so they
render in a browser and in Swagger's try-it-out. HTML tables (`?format=html`)
are the exception: they are sent as an attachment with `X-Content-Type-Options:
nosniff`, since that content comes from the converted document and is served
from the API's own origin.

### Pages

`GET /v1/documents/{id}/pages` returns every page with its content inline;
`GET /v1/documents/{id}/pages/{page_no}` returns one.

```json
{
  "page": 3,
  "width": 612.0,
  "height": 792.0,
  "markdown": "## Revenue\n\nRevenue held steady...",
  "text": "Revenue\n\nRevenue held steady...",
  "images": [ { "name": "picture-002.png", "kind": "chart", ... } ],
  "tables": [ { "index": 1, "rows": 4, "cols": 3, ... } ]
}
```

A page's `images` and `tables` entries are exactly the `/images` and `/tables`
entries carrying that page number.

Two properties worth knowing before you build against this:

- **Pages do not always sum to the document.** Content docling could not place
  on a page appears in `/markdown` and in no page's export. If you need
  guaranteed-complete text, use `/markdown` or `document.json` — not a
  concatenation of pages.
- **`/pages` is unbounded.** Response size grows with the document; a long scan
  produces a large response. This is deliberate, so that one request gets the
  whole document page by page. Use `/pages/{page_no}` where that matters.

`markdown` and `text` are `null` only when a page is listed but its file could
not be read. A page docling registered but extracted nothing from returns `""`.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `API_KEYS` | *(required)* | Comma-separated valid keys |
| `DATA_DIR` | `/data` | Volume root for SQLite and artifacts |
| `MODELS_DIR` | *(none; the shipped image sets it to `/opt/models`)* | Baked-in docling weights; unset means docling downloads weights at runtime |
| `WORKER_THREADS` | `2` | Concurrent conversions |
| `QUEUE_MAX` | `32` | Queue capacity before 503 |
| `JOB_TIMEOUT_SECONDS` | `600` | Per-document docling timeout |
| `CONVERTER_CACHE_SIZE` | `4` | Cached converters per worker thread |
| `SHUTDOWN_GRACE_SECONDS` | `30` | Seconds to let in-flight jobs finish on shutdown before the pool is forced to stop |
| `MAX_UPLOAD_BYTES` | `209715200` (200 MiB) | Requests declaring a larger `Content-Length` are refused with **413** before the body is read |
| `DEFAULT_OCR` | `true` | Default when the request omits it |
| `DEFAULT_OCR_LANGUAGES` | `en` | Default when the request omits it |
| `DEFAULT_TABLE_MODE` | `accurate` | Default when the request omits it |
| `DEFAULT_IMAGE_SCALE` | `2.0` | Default when the request omits it |
| `DOCS_ENABLED` | `true` | Serve `/docs`, `/redoc`, `/openapi.json` |
| `LOG_LEVEL` | `INFO` | Logging |

## Operational notes

- **One uvicorn worker only.** The queue and thread pool live in the process.
  Scale with `WORKER_THREADS`, not `--workers`.
- **Nothing is cleaned up automatically.** Deleting jobs is manual, via
  `DELETE /v1/documents/{id}`. Watch the volume.
- **Uploads are capped by `MAX_UPLOAD_BYTES`** (200 MiB by default). The check
  runs in ASGI middleware, ahead of routing, because FastAPI reads the request
  body before it resolves dependencies — so without it an unauthenticated
  client could spool an unbounded upload to disk against a route that answers
  401. A request that declares no `Content-Length` (chunked transfer) is passed
  through; bound those at your proxy. Within the limit, a very large document
  can still occupy a worker for a long time.
- **Nothing enumerates the volume.** There is no list route and no TTL sweeper,
  so the only way to see what is on disk is filesystem access — `ls $DATA_DIR/jobs`
  or a query against `$DATA_DIR/jobs.sqlite3`. Reclaiming space for jobs whose
  ids you have lost means deleting directories directly, not calling the API.
- **Logs are plain text, not structured.** One line per event via the stdlib
  `logging` module (`%(asctime)s %(levelname)s %(name)s %(message)s`). There are
  no key-value or JSON fields to index, so a log pipeline will need to parse
  them, and job ids are embedded in message text rather than carried as
  attributes.
- **Restart behavior:** jobs that were running are marked `failed` with type
  `interrupted`; jobs still queued are re-queued and run.
- **The shipped image bakes in Latin-script OCR weights only.** The
  Dockerfile runs `docling-tools models download ... easyocr
  --easyocr-lang en --easyocr-lang de` (only the models this app actually
  uses — layout, tableformer, easyocr — not docling's much larger default
  set). EasyOCR picks a recognizer from the whole language *set* rather
  than per language: `["en"]` exactly uses `english_g2.pth`, and every
  other Latin-script set — `["de"]`, `["en","de"]`, `["fr"]` — uses the
  single `latin_g2.pth`, which covers de, fr, es, it, nl, pt and the rest.
  Both are baked in, so any Latin-script request works offline.

  Non-Latin scripts (Cyrillic, Chinese, Japanese, Korean, Arabic,
  Devanagari, Thai…) are separate weight files and are **not** shipped.
  Because the container runs fully offline, they cannot be fetched at
  conversion time — so `POST /v1/documents` rejects them up front with
  **400 `unsupported_ocr_language`**, naming the weight file that is
  missing, instead of accepting the job and failing it in a worker. To
  enable one, rebuild with another `--easyocr-lang` flag. The same 400
  covers language codes EasyOCR does not know, and script combinations
  EasyOCR refuses (its non-Latin models pair only with English, so
  `["ja","de"]` is impossible regardless of what is installed).

  Requests with `"ocr": false` are never rejected on language grounds — no
  OCR model is constructed, so `ocr_languages` is inert.

## Development

```bash
/opt/homebrew/opt/python@3.12/bin/python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest                # fast suite; no models, no torch
```

The slow end-to-end test needs real docling and weights — run it in the
container, against the dedicated `test` image (a superset of the runtime
image with `tests/` and `pytest`/`httpx` added; the deployed `api` image
never carries either):

```bash
docker compose run --rm -e API_KEYS=test test pytest -m "" tests/test_end_to_end.py -v
```

**Building without compose:** the `test` stage is last in the Dockerfile, so a
bare `docker build .` builds *that* — the image with `tests/` and pytest in it,
not the deployable one. Pass the stage explicitly:

```bash
docker build --target runtime -t docling-api .   # the deployable image
docker build --target test    -t docling-api-test .
```

`docker compose` already names the stage for each service (`target: runtime`
for `api`, `target: test` for `test`), so this only applies to direct builds.

## Domain language

`CONTEXT.md` defines the vocabulary this project uses — what a *job* is versus
a *document*, why *picture* and *image* are both in play, and which of the two
kinds of *worker* any given sentence means.

## Design

- Spec: `docs/superpowers/specs/2026-09-19-docling-api-design.md`
- Plan: `docs/superpowers/plans/2026-09-19-docling-extraction-api.md`
