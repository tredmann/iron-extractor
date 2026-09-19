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

# 3. Fetch artifacts
curl http://localhost:8000/v1/documents/<id>/markdown       -H "X-API-Key: dev-key"
curl http://localhost:8000/v1/documents/<id>/document.json  -H "X-API-Key: dev-key"
curl "http://localhost:8000/v1/documents/<id>/tables/0?format=csv" -H "X-API-Key: dev-key"
curl http://localhost:8000/v1/documents/<id>/images/picture-000.png -H "X-API-Key: dev-key" -o picture.png

# 4. Reclaim the space
curl -X DELETE http://localhost:8000/v1/documents/<id> -H "X-API-Key: dev-key"
```

Artifact routes return **409** while a job is queued or running, and **404** if the
job does not exist — so "not ready" is always distinguishable from "not found".

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
- **There is no upload size limit.** A very large document can occupy a worker
  for a long time.
- **Restart behavior:** jobs that were running are marked `failed` with type
  `interrupted`; jobs still queued are re-queued and run.
- **The shipped image only bakes in English OCR weights.** The Dockerfile
  runs `docling-tools models download ... easyocr --easyocr-lang en` (only
  the models this app actually uses — layout, tableformer, easyocr — not
  docling's much larger default set). Since the container runs fully
  offline (`HF_HUB_OFFLINE=1`), requesting `ocr_languages` other than `en`
  will fail at conversion time unless the image is rebuilt with additional
  `--easyocr-lang` flags for the languages you need.

## Development

```bash
/opt/homebrew/opt/python@3.12/bin/python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest                # fast suite; no models, no torch
```

The slow end-to-end test needs real docling and weights — run it in the container:

```bash
docker compose run --rm -e API_KEYS=test api pytest -m "" tests/test_end_to_end.py -v
```

## Design

- Spec: `docs/superpowers/specs/2026-09-19-docling-api-design.md`
- Plan: `docs/superpowers/plans/2026-09-19-docling-extraction-api.md`
