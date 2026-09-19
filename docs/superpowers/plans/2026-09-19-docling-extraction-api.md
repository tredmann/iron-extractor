# Docling Extraction API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An async HTTP API that converts uploaded documents with docling — POST returns a UUID, separate routes serve markdown, DoclingDocument JSON, tables, and images — packaged as a single Docker container.

**Architecture:** One FastAPI process. Four layers that don't know about each other: `JobStore` (SQLite persistence), `ConversionService` (the only docling consumer), `WorkerPool` (bounded queue + worker threads), and the API layer. Job state and artifacts live on a mounted volume; model weights are baked into the image.

**Tech Stack:** Python 3.12, FastAPI, uvicorn, pydantic-settings, SQLite (stdlib `sqlite3`), docling 2.129.0, pandas, Pillow, pytest.

**Spec:** `docs/superpowers/specs/2026-09-19-docling-api-design.md`

## Global Constraints

- **Python 3.12.** docling 2.129.0 declares `>=3.10,<4.0`; torch wheels are not reliably available for 3.14. Local interpreter: `/opt/homebrew/opt/python@3.12/bin/python3.12`. Container base: `python:3.12-slim`.
- **docling is pinned to `docling==2.129.0`.** Every docling call in this plan was verified against that version. Do not upgrade it as a side effect of another task.
- **docling is imported lazily, inside functions, and only in `app/conversion/converters.py`.** No module-level `import docling` anywhere. This keeps the fast test suite runnable without torch installed, and means a docling upgrade touches one file.
- **`uvicorn` runs with exactly one worker.** The queue and thread pool live in the process; multiple uvicorn workers would mean multiple independent queues contending over one SQLite file.
- **Error envelope is always** `{"error": {"code": "...", "message": "..."}}` — every non-2xx response, no exceptions.
- **Timestamps** are ISO-8601 UTC strings produced by `datetime.now(timezone.utc).isoformat()`.
- **Test commands** run from the repo root with the venv active: `.venv/bin/pytest`.
- **Fast tests never load a model.** Only the single test marked `@pytest.mark.slow` touches real docling.

---

## Refinements to the spec

These were decided while verifying the docling API. They refine the spec rather than contradict it; each is implemented as described below.

1. **`queue.Queue`, not `asyncio.Queue`.** The spec says `asyncio.Queue`, but the consumers are OS threads and `asyncio.Queue` is not thread-safe. `queue.Queue(maxsize=QUEUE_MAX)` gives the same bounded-queue semantics and is safe across threads.
2. **Converter cache is a bounded LRU of size `CONVERTER_CACHE_SIZE` (default 4) per thread.** The spec said "rebuild past 8 pipelines". Each cached `DocumentConverter` holds live model pipelines, so 8 per thread × N threads is a lot of memory. An explicit small LRU is the same idea with a tighter, configurable bound.
3. **Non-timeout `PARTIAL_SUCCESS` counts as success**, with the docling error messages recorded in the manifest under `warnings`. A timeout `PARTIAL_SUCCESS` is a failure with type `timeout`. The spec did not cover partial success.
4. **The OCR engine is pinned to EasyOCR** rather than docling's default `OcrAutoOptions()`, so behavior is deterministic and the offline container never probes for an engine it doesn't have.
5. **Worker supervision is structural**: the thread body wraps every job in `try/except Exception`, so a bad job cannot kill a worker. No separate monitor thread.
6. **Requirements are split three ways** so the fast test suite installs without torch (~2 GB saved locally): `requirements-base.txt`, `requirements.txt` (base + docling), `requirements-dev.txt` (base + test tools).

---

## File Structure

| File | Responsibility |
|---|---|
| `app/config.py` | `Settings` from env via pydantic-settings |
| `app/api/errors.py` | `ApiError` + handlers producing the error envelope |
| `app/api/deps.py` | API-key dependency, app-state accessors |
| `app/api/schemas.py` | Request/response pydantic models, including `ConversionOptions` |
| `app/api/routes.py` | All HTTP routes |
| `app/jobs/models.py` | `JobStatus`, `Job` |
| `app/jobs/store.py` | `JobStore` — SQLite persistence |
| `app/jobs/pool.py` | `WorkerPool` — bounded queue + threads |
| `app/storage/paths.py` | `JobPaths` — job directory layout, filename sanitization |
| `app/conversion/artifacts.py` | Writes artifacts from a `DoclingDocument`, returns the manifest |
| `app/conversion/converters.py` | **The only docling importer.** Per-thread converter LRU, options → pipeline options |
| `app/conversion/service.py` | `ConversionService` — orchestrates convert → write artifacts |
| `app/main.py` | App factory, lifespan wiring |
| `tests/conftest.py` | Shared fixtures: settings, temp data dir, fake service, client |
| `Dockerfile`, `docker-compose.yml`, `README.md` | Packaging and docs |

---

### Task 1: Project scaffold, settings, error envelope, health

**Files:**
- Create: `requirements-base.txt`, `requirements.txt`, `requirements-dev.txt`, `pytest.ini`, `.gitignore`
- Create: `app/__init__.py`, `app/config.py`, `app/api/__init__.py`, `app/api/errors.py`, `app/main.py`
- Test: `tests/__init__.py`, `tests/test_config.py`, `tests/test_health.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `app.config.Settings` — pydantic-settings model; `get_settings() -> Settings` (cached).
  - `Settings` fields: `api_keys: str`, `data_dir: Path`, `worker_threads: int`, `queue_max: int`, `job_timeout_seconds: float`, `converter_cache_size: int`, `models_dir: Path | None`, `default_ocr: bool`, `default_ocr_languages: str`, `default_table_mode: str`, `default_image_scale: float`, `docs_enabled: bool`, `log_level: str`, `shutdown_grace_seconds: float`.
  - `Settings.api_key_list -> list[str]` and `Settings.default_ocr_language_list -> list[str]` (comma-split, stripped, empties dropped).
  - `app.api.errors.ApiError(status_code: int, code: str, message: str)` — exception.
  - `app.api.errors.install_error_handlers(app: FastAPI) -> None`.
  - `app.main.create_app(settings: Settings | None = None) -> FastAPI`.

- [ ] **Step 1: Create the virtualenv and requirements files**

```bash
/opt/homebrew/opt/python@3.12/bin/python3.12 -m venv .venv
```

`requirements-base.txt`:

```
fastapi==0.120.4
uvicorn[standard]==0.41.0
pydantic==2.12.4
pydantic-settings==2.13.0
python-multipart==0.0.21
docling-core==2.52.0
pandas==2.3.4
pillow==12.1.0
```

`requirements.txt`:

```
-r requirements-base.txt
docling==2.129.0
```

`requirements-dev.txt`:

```
-r requirements-base.txt
pytest==9.1.0
pytest-asyncio==1.3.0
httpx==0.29.0
ruff==0.15.4
```

> If any pinned version above fails to resolve for Python 3.12, install the package without the pin, then run `.venv/bin/pip freeze | grep -i <package>` and write the resolved version back into the file. Never leave a dependency unpinned.

`.gitignore`:

```
.venv/
__pycache__/
*.pyc
.pytest_cache/
data/
scratch/
```

`pytest.ini`:

```ini
[pytest]
testpaths = tests
markers =
    slow: requires real docling and model weights (deselect with '-m "not slow"')
addopts = -m "not slow"
asyncio_mode = auto
```

- [ ] **Step 2: Install dev dependencies**

```bash
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements-dev.txt
```

Expected: installs without pulling torch.

- [ ] **Step 3: Write the failing tests**

`tests/__init__.py` — empty file.

`tests/test_config.py`:

```python
from pathlib import Path

from app.config import Settings


def test_api_key_list_splits_and_strips():
    settings = Settings(api_keys="alpha, beta ,,gamma", data_dir=Path("/tmp/x"))
    assert settings.api_key_list == ["alpha", "beta", "gamma"]


def test_default_ocr_language_list_splits():
    settings = Settings(api_keys="k", data_dir=Path("/tmp/x"), default_ocr_languages="en, de")
    assert settings.default_ocr_language_list == ["en", "de"]


def test_defaults_match_spec():
    settings = Settings(api_keys="k", data_dir=Path("/tmp/x"))
    assert settings.worker_threads == 2
    assert settings.queue_max == 32
    assert settings.job_timeout_seconds == 600.0
    assert settings.converter_cache_size == 4
    assert settings.default_ocr is True
    assert settings.default_table_mode == "accurate"
    assert settings.default_image_scale == 2.0
    assert settings.docs_enabled is True
```

`tests/test_health.py`:

```python
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_health_is_unauthenticated_and_ok(tmp_path: Path):
    app = create_app(Settings(api_keys="secret", data_dir=tmp_path))
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_unknown_route_uses_error_envelope(tmp_path: Path):
    app = create_app(Settings(api_keys="secret", data_dir=tmp_path))
    with TestClient(app) as client:
        response = client.get("/nope")
    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "not_found", "message": "Not Found"}
    }
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_config.py tests/test_health.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app'`

- [ ] **Step 5: Implement `app/config.py`**

`app/__init__.py` and `app/api/__init__.py` — empty files.

```python
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


class Settings(BaseSettings):
    """Runtime configuration, read from the environment."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    api_keys: str
    data_dir: Path = Path("/data")
    models_dir: Path | None = None

    worker_threads: int = 2
    queue_max: int = 32
    job_timeout_seconds: float = 600.0
    converter_cache_size: int = 4
    shutdown_grace_seconds: float = 30.0

    default_ocr: bool = True
    default_ocr_languages: str = "en"
    default_table_mode: str = "accurate"
    default_image_scale: float = 2.0

    docs_enabled: bool = True
    log_level: str = "INFO"

    @property
    def api_key_list(self) -> list[str]:
        return _split_csv(self.api_keys)

    @property
    def default_ocr_language_list(self) -> list[str]:
        return _split_csv(self.default_ocr_languages)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobs.sqlite3"


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 6: Implement `app/api/errors.py`**

```python
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

_STATUS_CODES = {
    400: "invalid_request",
    401: "unauthorized",
    404: "not_found",
    409: "conflict",
    503: "unavailable",
}


class ApiError(Exception):
    """An error that renders as the API's standard error envelope."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _envelope(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _handle_api_error(_: Request, exc: ApiError) -> JSONResponse:
        return _envelope(exc.status_code, exc.code, exc.message)

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, "error")
        return _envelope(exc.status_code, code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _envelope(400, "invalid_request", _first_validation_message(exc))

    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error", exc_info=exc)
        return _envelope(500, "internal_error", "An unexpected error occurred.")


def _first_validation_message(exc: RequestValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "Request validation failed."
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
    message = first.get("msg", "invalid value")
    return f"{location}: {message}" if location else message
```

- [ ] **Step 7: Implement `app/main.py`**

```python
import logging

from fastapi import APIRouter, FastAPI

from app.api.errors import install_error_handlers
from app.config import Settings, get_settings

health_router = APIRouter()


@health_router.get("/health", tags=["health"], summary="Liveness and readiness probe")
async def health() -> dict:
    return {"status": "ok"}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level.upper())

    app = FastAPI(
        title="Docling Extraction API",
        version="0.1.0",
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    app.state.settings = settings
    install_error_handlers(app)
    app.include_router(health_router)
    return app
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_config.py tests/test_health.py -v`
Expected: PASS (5 tests)

- [ ] **Step 9: Commit**

```bash
git add requirements-base.txt requirements.txt requirements-dev.txt pytest.ini .gitignore app tests
git commit -m "feat: scaffold FastAPI app with settings, error envelope, and health route"
```

---

### Task 2: API key authentication

**Files:**
- Create: `app/api/deps.py`
- Test: `tests/test_auth.py`

**Interfaces:**
- Consumes: `Settings.api_key_list`, `ApiError` (Task 1).
- Produces:
  - `app.api.deps.api_key_header` — `APIKeyHeader(name="X-API-Key", auto_error=False)`, reused by the OpenAPI task so the security scheme is declared once.
  - `app.api.deps.get_settings_from_request(request: Request) -> Settings`.
  - `app.api.deps.require_api_key(...)` — FastAPI dependency raising `ApiError(401, "unauthorized", ...)`.

- [ ] **Step 1: Write the failing test**

`tests/test_auth.py`:

```python
from pathlib import Path

from fastapi import Depends
from fastapi.testclient import TestClient

from app.api.deps import require_api_key
from app.config import Settings
from app.main import create_app


def _client(tmp_path: Path) -> TestClient:
    app = create_app(Settings(api_keys="key-one,key-two", data_dir=tmp_path))

    @app.get("/protected", dependencies=[Depends(require_api_key)])
    async def protected() -> dict:
        return {"ok": True}

    return TestClient(app)


def test_valid_key_is_accepted(tmp_path: Path):
    with _client(tmp_path) as client:
        response = client.get("/protected", headers={"X-API-Key": "key-two"})
    assert response.status_code == 200


def test_missing_key_is_rejected(tmp_path: Path):
    with _client(tmp_path) as client:
        response = client.get("/protected")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_wrong_key_is_rejected(tmp_path: Path):
    with _client(tmp_path) as client:
        response = client.get("/protected", headers={"X-API-Key": "nope"})
    assert response.status_code == 401


def test_health_stays_open(tmp_path: Path):
    with _client(tmp_path) as client:
        assert client.get("/health").status_code == 200
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.api.deps'`

- [ ] **Step 3: Implement `app/api/deps.py`**

```python
from secrets import compare_digest

from fastapi import Request, Security
from fastapi.security import APIKeyHeader

from app.api.errors import ApiError
from app.config import Settings

api_key_header = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    description="Static API key. Configure valid keys with the API_KEYS environment variable.",
)


def get_settings_from_request(request: Request) -> Settings:
    return request.app.state.settings


def require_api_key(
    request: Request,
    provided: str | None = Security(api_key_header),
) -> None:
    """Reject the request unless it carries a configured API key."""
    settings = get_settings_from_request(request)
    if provided is None:
        raise ApiError(401, "unauthorized", "Missing X-API-Key header.")
    # compare_digest against every key so the check is constant-time per key.
    if not any(compare_digest(provided, key) for key in settings.api_key_list):
        raise ApiError(401, "unauthorized", "Invalid API key.")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_auth.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add app/api/deps.py tests/test_auth.py
git commit -m "feat: add API key authentication dependency"
```

---

### Task 3: Job directory layout (`JobPaths`)

**Files:**
- Create: `app/storage/__init__.py`, `app/storage/paths.py`
- Test: `tests/test_paths.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `app.storage.paths.JobPaths` with `job_dir`, `input_dir`, `output_dir`, `tables_dir`, `images_dir`, `markdown_file`, `document_file`, `table_file(job_id, index, fmt)`, `image_file(job_id, name)`, `create(job_id)`, `remove(job_id)`, `input_file(job_id, filename)`, and the module function `sanitize_filename(raw: str) -> str`.

- [ ] **Step 1: Write the failing test**

`tests/test_paths.py`:

```python
from pathlib import Path

import pytest

from app.storage.paths import JobPaths, sanitize_filename


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("../../etc/passwd", "passwd"),
        ("/absolute/path/doc.pdf", "doc.pdf"),
        ("", "upload"),
        ("   ", "upload"),
        ("..", "upload"),
        ("with\x00null.pdf", "withnull.pdf"),
    ],
)
def test_sanitize_filename(raw: str, expected: str):
    assert sanitize_filename(raw) == expected


def test_create_makes_the_directory_tree(tmp_path: Path):
    paths = JobPaths(tmp_path)
    paths.create("job-1")
    assert paths.input_dir("job-1").is_dir()
    assert paths.tables_dir("job-1").is_dir()
    assert paths.images_dir("job-1").is_dir()


def test_remove_deletes_everything(tmp_path: Path):
    paths = JobPaths(tmp_path)
    paths.create("job-1")
    paths.markdown_file("job-1").write_text("hello")
    paths.remove("job-1")
    assert not paths.job_dir("job-1").exists()


def test_remove_is_idempotent(tmp_path: Path):
    JobPaths(tmp_path).remove("never-existed")


def test_input_file_sanitizes(tmp_path: Path):
    paths = JobPaths(tmp_path)
    resolved = paths.input_file("job-1", "../../evil.pdf")
    assert resolved == paths.input_dir("job-1") / "evil.pdf"


def test_image_file_rejects_traversal(tmp_path: Path):
    paths = JobPaths(tmp_path)
    with pytest.raises(ValueError):
        paths.image_file("job-1", "../../../etc/passwd")


def test_table_file_naming(tmp_path: Path):
    paths = JobPaths(tmp_path)
    assert paths.table_file("job-1", 7, "csv").name == "table-007.csv"
    assert paths.table_file("job-1", 7, "html").name == "table-007.html"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_paths.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.storage'`

- [ ] **Step 3: Implement `app/storage/paths.py`**

`app/storage/__init__.py` — empty file.

```python
import shutil
from pathlib import Path


def sanitize_filename(raw: str) -> str:
    """Reduce an uploaded filename to a safe basename."""
    cleaned = raw.replace("\x00", "").strip()
    name = Path(cleaned).name.strip()
    if not name or name in {".", ".."}:
        return "upload"
    return name


class JobPaths:
    """Resolves every on-disk location belonging to a job."""

    def __init__(self, data_dir: Path) -> None:
        self.jobs_root = Path(data_dir) / "jobs"

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_root / job_id

    def input_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "input"

    def output_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "output"

    def tables_dir(self, job_id: str) -> Path:
        return self.output_dir(job_id) / "tables"

    def images_dir(self, job_id: str) -> Path:
        return self.output_dir(job_id) / "images"

    def markdown_file(self, job_id: str) -> Path:
        return self.output_dir(job_id) / "document.md"

    def document_file(self, job_id: str) -> Path:
        return self.output_dir(job_id) / "document.json"

    def table_file(self, job_id: str, index: int, fmt: str) -> Path:
        return self.tables_dir(job_id) / f"table-{index:03d}.{fmt}"

    def image_file(self, job_id: str, name: str) -> Path:
        """Resolve an image by name, refusing anything that escapes the job."""
        images_dir = self.images_dir(job_id)
        candidate = (images_dir / name).resolve()
        if candidate.parent != images_dir.resolve():
            raise ValueError(f"Illegal image name: {name!r}")
        return candidate

    def input_file(self, job_id: str, filename: str) -> Path:
        return self.input_dir(job_id) / sanitize_filename(filename)

    def create(self, job_id: str) -> None:
        for directory in (
            self.input_dir(job_id),
            self.output_dir(job_id),
            self.tables_dir(job_id),
            self.images_dir(job_id),
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def remove(self, job_id: str) -> None:
        shutil.rmtree(self.job_dir(job_id), ignore_errors=True)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_paths.py -v`
Expected: PASS (13 tests)

- [ ] **Step 5: Commit**

```bash
git add app/storage tests/test_paths.py
git commit -m "feat: add job directory layout with filename sanitization"
```

---

### Task 4: Job persistence (`JobStore`)

**Files:**
- Create: `app/jobs/__init__.py`, `app/jobs/models.py`, `app/jobs/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `JobPaths` (Task 3).
- Produces:
  - `app.jobs.models.JobStatus` — `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED` (str enum, values `"queued"`, `"running"`, `"succeeded"`, `"failed"`).
  - `app.jobs.models.Job` — frozen dataclass: `id, status, filename, options, created_at, started_at, finished_at, error_type, error_message, manifest`.
  - `app.jobs.store.JobStore(db_path: Path, paths: JobPaths)` with `connect()`, `close()`, `create(job_id, filename, options) -> Job`, `get(job_id) -> Job | None`, `mark_running(job_id)`, `mark_succeeded(job_id, manifest)`, `mark_failed(job_id, error_type, error_message)`, `delete(job_id) -> bool`, `recover_interrupted() -> list[str]`.

`recover_interrupted()` marks every `running` job `failed` with type `interrupted` and returns the ids of jobs still `queued`, so the caller can re-enqueue them.

- [ ] **Step 1: Write the failing test**

`tests/test_store.py`:

```python
from pathlib import Path

import pytest

from app.jobs.models import JobStatus
from app.jobs.store import JobStore
from app.storage.paths import JobPaths


@pytest.fixture
def store(tmp_path: Path):
    paths = JobPaths(tmp_path)
    store = JobStore(tmp_path / "jobs.sqlite3", paths)
    store.connect()
    yield store
    store.close()


def test_create_then_get_roundtrips(store: JobStore):
    created = store.create("job-1", "report.pdf", {"ocr": True})
    assert created.status is JobStatus.QUEUED
    fetched = store.get("job-1")
    assert fetched is not None
    assert fetched.filename == "report.pdf"
    assert fetched.options == {"ocr": True}
    assert fetched.created_at
    assert fetched.started_at is None
    assert fetched.manifest is None


def test_get_unknown_returns_none(store: JobStore):
    assert store.get("missing") is None


def test_status_transitions(store: JobStore):
    store.create("job-1", "a.pdf", {})
    store.mark_running("job-1")
    running = store.get("job-1")
    assert running.status is JobStatus.RUNNING
    assert running.started_at is not None

    store.mark_succeeded("job-1", {"page_count": 3})
    done = store.get("job-1")
    assert done.status is JobStatus.SUCCEEDED
    assert done.manifest == {"page_count": 3}
    assert done.finished_at is not None


def test_mark_failed_records_error(store: JobStore):
    store.create("job-1", "a.pdf", {})
    store.mark_failed("job-1", "timeout", "took too long")
    failed = store.get("job-1")
    assert failed.status is JobStatus.FAILED
    assert failed.error_type == "timeout"
    assert failed.error_message == "took too long"
    assert failed.finished_at is not None


def test_delete_removes_row_and_directory(tmp_path: Path, store: JobStore):
    paths = JobPaths(tmp_path)
    paths.create("job-1")
    store.create("job-1", "a.pdf", {})

    assert store.delete("job-1") is True
    assert store.get("job-1") is None
    assert not paths.job_dir("job-1").exists()


def test_delete_unknown_returns_false(store: JobStore):
    assert store.delete("missing") is False


def test_recover_interrupted(store: JobStore):
    store.create("queued-job", "a.pdf", {})
    store.create("running-job", "b.pdf", {})
    store.mark_running("running-job")
    store.create("done-job", "c.pdf", {})
    store.mark_succeeded("done-job", {})

    requeued = store.recover_interrupted()

    assert requeued == ["queued-job"]
    interrupted = store.get("running-job")
    assert interrupted.status is JobStatus.FAILED
    assert interrupted.error_type == "interrupted"
    assert store.get("done-job").status is JobStatus.SUCCEEDED


def test_survives_reopen(tmp_path: Path):
    paths = JobPaths(tmp_path)
    first = JobStore(tmp_path / "jobs.sqlite3", paths)
    first.connect()
    first.create("job-1", "a.pdf", {})
    first.close()

    second = JobStore(tmp_path / "jobs.sqlite3", paths)
    second.connect()
    assert second.get("job-1") is not None
    second.close()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.jobs'`

- [ ] **Step 3: Implement `app/jobs/models.py`**

`app/jobs/__init__.py` — empty file.

```python
from dataclasses import dataclass
from enum import Enum


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in {JobStatus.SUCCEEDED, JobStatus.FAILED}


@dataclass(frozen=True)
class Job:
    id: str
    status: JobStatus
    filename: str
    options: dict
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    manifest: dict | None = None
```

- [ ] **Step 4: Implement `app/jobs/store.py`**

```python
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.jobs.models import Job, JobStatus
from app.storage.paths import JobPaths

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    status        TEXT NOT NULL,
    filename      TEXT NOT NULL,
    options       TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    error_type    TEXT,
    error_message TEXT,
    manifest      TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs (status);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    """SQLite-backed job metadata. Safe to call from worker threads."""

    def __init__(self, db_path: Path, paths: JobPaths) -> None:
        self._db_path = Path(db_path)
        self._paths = paths
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False because worker threads write; every write is
        # serialized by self._lock.
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("JobStore.connect() has not been called")
        return self._conn

    def create(self, job_id: str, filename: str, options: dict) -> Job:
        job = Job(
            id=job_id,
            status=JobStatus.QUEUED,
            filename=filename,
            options=options,
            created_at=_now(),
        )
        with self._lock:
            self._db.execute(
                "INSERT INTO jobs (id, status, filename, options, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (job.id, job.status.value, job.filename, json.dumps(job.options), job.created_at),
            )
            self._db.commit()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row is not None else None

    def mark_running(self, job_id: str) -> None:
        self._update(
            "UPDATE jobs SET status = ?, started_at = ? WHERE id = ?",
            (JobStatus.RUNNING.value, _now(), job_id),
        )

    def mark_succeeded(self, job_id: str, manifest: dict) -> None:
        self._update(
            "UPDATE jobs SET status = ?, finished_at = ?, manifest = ? WHERE id = ?",
            (JobStatus.SUCCEEDED.value, _now(), json.dumps(manifest), job_id),
        )

    def mark_failed(self, job_id: str, error_type: str, error_message: str) -> None:
        self._update(
            "UPDATE jobs SET status = ?, finished_at = ?, error_type = ?, error_message = ?"
            " WHERE id = ?",
            (JobStatus.FAILED.value, _now(), error_type, error_message, job_id),
        )

    def delete(self, job_id: str) -> bool:
        with self._lock:
            cursor = self._db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            self._db.commit()
            deleted = cursor.rowcount > 0
        self._paths.remove(job_id)
        return deleted

    def recover_interrupted(self) -> list[str]:
        """Fail jobs abandoned mid-run; return queued job ids to re-enqueue."""
        with self._lock:
            self._db.execute(
                "UPDATE jobs SET status = ?, finished_at = ?, error_type = ?,"
                " error_message = ? WHERE status = ?",
                (
                    JobStatus.FAILED.value,
                    _now(),
                    "interrupted",
                    "The service restarted while this job was running.",
                    JobStatus.RUNNING.value,
                ),
            )
            rows = self._db.execute(
                "SELECT id FROM jobs WHERE status = ? ORDER BY created_at",
                (JobStatus.QUEUED.value,),
            ).fetchall()
            self._db.commit()
        return [row["id"] for row in rows]

    def _update(self, sql: str, params: tuple) -> None:
        with self._lock:
            self._db.execute(sql, params)
            self._db.commit()


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        status=JobStatus(row["status"]),
        filename=row["filename"],
        options=json.loads(row["options"]),
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        error_type=row["error_type"],
        error_message=row["error_message"],
        manifest=json.loads(row["manifest"]) if row["manifest"] else None,
    )
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_store.py -v`
Expected: PASS (8 tests)

- [ ] **Step 6: Commit**

```bash
git add app/jobs tests/test_store.py
git commit -m "feat: add SQLite job store with crash recovery"
```

---

### Task 5: Conversion options schema

**Files:**
- Create: `app/api/schemas.py`
- Test: `tests/test_options.py`

**Interfaces:**
- Consumes: `Settings` (Task 1), `ApiError` (Task 1).
- Produces:
  - `app.api.schemas.TableMode` — str enum `FAST = "fast"`, `ACCURATE = "accurate"`.
  - `app.api.schemas.ConversionOptions` — pydantic model with `ocr: bool`, `ocr_languages: list[str]`, `table_mode: TableMode`, `image_scale: float`; `extra="forbid"`; `image_scale` clamped to 1.0–4.0 then quantized to 0.5 steps.
  - `ConversionOptions.cache_key() -> tuple` — used by the converter LRU in Task 7.
  - `app.api.schemas.parse_options(raw: str | None, settings: Settings) -> ConversionOptions` — fills defaults from settings, raises `ApiError(400, "invalid_request", ...)` on bad JSON or bad values.

- [ ] **Step 1: Write the failing test**

`tests/test_options.py`:

```python
import json
from pathlib import Path

import pytest

from app.api.errors import ApiError
from app.api.schemas import ConversionOptions, TableMode, parse_options
from app.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(api_keys="k", data_dir=tmp_path)


def test_defaults_come_from_settings(settings: Settings):
    options = parse_options(None, settings)
    assert options.ocr is True
    assert options.ocr_languages == ["en"]
    assert options.table_mode is TableMode.ACCURATE
    assert options.image_scale == 2.0


def test_explicit_values_override_defaults(settings: Settings):
    options = parse_options(
        json.dumps({"ocr": False, "ocr_languages": ["de"], "table_mode": "fast"}),
        settings,
    )
    assert options.ocr is False
    assert options.ocr_languages == ["de"]
    assert options.table_mode is TableMode.FAST


def test_blank_string_is_treated_as_absent(settings: Settings):
    assert parse_options("   ", settings).image_scale == 2.0


@pytest.mark.parametrize(
    ("given", "expected"),
    [(0.1, 1.0), (1.0, 1.0), (1.2, 1.0), (1.3, 1.5), (2.4, 2.5), (9.0, 4.0)],
)
def test_image_scale_is_clamped_and_quantized(settings: Settings, given, expected):
    options = parse_options(json.dumps({"image_scale": given}), settings)
    assert options.image_scale == expected


def test_unknown_field_is_rejected(settings: Settings):
    with pytest.raises(ApiError) as excinfo:
        parse_options(json.dumps({"colour": "blue"}), settings)
    assert excinfo.value.status_code == 400


def test_bad_table_mode_is_rejected(settings: Settings):
    with pytest.raises(ApiError):
        parse_options(json.dumps({"table_mode": "perfect"}), settings)


def test_malformed_json_is_rejected(settings: Settings):
    with pytest.raises(ApiError) as excinfo:
        parse_options("{not json", settings)
    assert "JSON" in excinfo.value.message


def test_non_object_json_is_rejected(settings: Settings):
    with pytest.raises(ApiError):
        parse_options("[1, 2]", settings)


def test_empty_language_list_is_rejected(settings: Settings):
    with pytest.raises(ApiError):
        parse_options(json.dumps({"ocr_languages": []}), settings)


def test_blank_language_is_rejected(settings: Settings):
    with pytest.raises(ApiError):
        parse_options(json.dumps({"ocr_languages": ["  "]}), settings)


def test_cache_key_distinguishes_option_sets():
    first = ConversionOptions(ocr=True, ocr_languages=["en"], table_mode=TableMode.FAST, image_scale=2.0)
    same = ConversionOptions(ocr=True, ocr_languages=["en"], table_mode=TableMode.FAST, image_scale=2.0)
    other = ConversionOptions(ocr=True, ocr_languages=["de"], table_mode=TableMode.FAST, image_scale=2.0)
    assert first.cache_key() == same.cache_key()
    assert first.cache_key() != other.cache_key()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_options.py -v`
Expected: FAIL — `ImportError: cannot import name 'ConversionOptions'`

- [ ] **Step 3: Implement `app/api/schemas.py`**

```python
import json
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.api.errors import ApiError
from app.config import Settings

MIN_IMAGE_SCALE = 1.0
MAX_IMAGE_SCALE = 4.0
IMAGE_SCALE_STEP = 0.5


class TableMode(str, Enum):
    FAST = "fast"
    ACCURATE = "accurate"


class ConversionOptions(BaseModel):
    """Per-request docling knobs. Unknown fields are rejected, not ignored."""

    model_config = ConfigDict(extra="forbid")

    ocr: bool = Field(default=True, description="Run OCR over the document.")
    ocr_languages: list[str] = Field(
        default=["en"], description="OCR language codes, e.g. ['en', 'de']."
    )
    table_mode: TableMode = Field(
        default=TableMode.ACCURATE,
        description="TableFormer mode. 'accurate' is slower and higher quality.",
    )
    image_scale: float = Field(
        default=2.0,
        description=(
            "Rendering scale for extracted images (1.0 ~ 72 DPI). "
            "Clamped to 1.0-4.0 and rounded to the nearest 0.5."
        ),
    )

    @field_validator("ocr_languages")
    @classmethod
    def _validate_languages(cls, value: list[str]) -> list[str]:
        languages = [item.strip() for item in value if item.strip()]
        if not languages:
            raise ValueError("must contain at least one non-empty language code")
        return languages

    @field_validator("image_scale")
    @classmethod
    def _clamp_and_quantize(cls, value: float) -> float:
        clamped = min(max(value, MIN_IMAGE_SCALE), MAX_IMAGE_SCALE)
        # Quantizing bounds the converter cache key space (see Task 7).
        return round(round(clamped / IMAGE_SCALE_STEP) * IMAGE_SCALE_STEP, 1)

    def cache_key(self) -> tuple:
        return (self.ocr, tuple(self.ocr_languages), self.table_mode.value, self.image_scale)


def parse_options(raw: str | None, settings: Settings) -> ConversionOptions:
    """Build options from a raw JSON request field, defaulting from settings."""
    values: dict = {
        "ocr": settings.default_ocr,
        "ocr_languages": settings.default_ocr_language_list,
        "table_mode": settings.default_table_mode,
        "image_scale": settings.default_image_scale,
    }

    if raw is not None and raw.strip():
        try:
            supplied = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ApiError(400, "invalid_request", f"options is not valid JSON: {exc.msg}") from exc
        if not isinstance(supplied, dict):
            raise ApiError(400, "invalid_request", "options must be a JSON object.")
        values.update(supplied)

    try:
        return ConversionOptions(**values)
    except ValidationError as exc:
        raise ApiError(400, "invalid_request", _first_error(exc)) from exc


def _first_error(exc: ValidationError) -> str:
    first = exc.errors()[0]
    location = ".".join(str(part) for part in first.get("loc", ()))
    message = first.get("msg", "invalid value")
    return f"options.{location}: {message}" if location else f"options: {message}"
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_options.py -v`
Expected: PASS (16 tests)

- [ ] **Step 5: Commit**

```bash
git add app/api/schemas.py tests/test_options.py
git commit -m "feat: add validated conversion options schema"
```

---

### Task 6: Artifact writer and manifest

**Files:**
- Create: `app/conversion/__init__.py`, `app/conversion/artifacts.py`
- Test: `tests/test_artifacts.py`

**Interfaces:**
- Consumes: `JobPaths` (Task 3).
- Produces: `app.conversion.artifacts.write_artifacts(document, paths: JobPaths, job_id: str, warnings: list[str] | None = None) -> dict`.

Manifest shape (filenames only — the API layer builds URLs from them):

```python
{
  "page_count": 2,
  "warnings": [],
  "markdown": {"bytes": 42},
  "document": {"bytes": 8123},
  "tables": [{"index": 0, "rows": 2, "cols": 2, "page": 1,
              "csv": "table-000.csv", "html": "table-000.html"}],
  "images": [{"name": "picture-000.png", "kind": "picture", "page": 2, "bytes": 190}],
}
```

This module depends only on `docling-core` (the `DoclingDocument` type), never on `docling` itself, so it is testable with a hand-built document and no models.

- [ ] **Step 1: Write the failing test**

`tests/test_artifacts.py`:

```python
import json
from pathlib import Path

import pytest
from docling_core.types.doc import (
    DoclingDocument,
    ImageRef,
    ProvenanceItem,
    Size,
    TableCell,
    TableData,
)
from docling_core.types.doc.base import BoundingBox, CoordOrigin
from PIL import Image

from app.conversion.artifacts import write_artifacts
from app.storage.paths import JobPaths


def _prov(page_no: int) -> ProvenanceItem:
    return ProvenanceItem(
        page_no=page_no,
        bbox=BoundingBox(l=0, t=0, r=10, b=10, coord_origin=CoordOrigin.TOPLEFT),
        charspan=(0, 0),
    )


@pytest.fixture
def document() -> DoclingDocument:
    doc = DoclingDocument(name="fixture")
    doc.add_page(page_no=1, size=Size(width=600, height=800))
    doc.add_page(page_no=2, size=Size(width=600, height=800))
    doc.add_title(text="Fixture Document")
    doc.add_text(label="text", text="Some body copy.")

    cells = [
        TableCell(text="h1", start_row_offset_idx=0, end_row_offset_idx=1,
                  start_col_offset_idx=0, end_col_offset_idx=1, column_header=True),
        TableCell(text="h2", start_row_offset_idx=0, end_row_offset_idx=1,
                  start_col_offset_idx=1, end_col_offset_idx=2, column_header=True),
        TableCell(text="a", start_row_offset_idx=1, end_row_offset_idx=2,
                  start_col_offset_idx=0, end_col_offset_idx=1),
        TableCell(text="b", start_row_offset_idx=1, end_row_offset_idx=2,
                  start_col_offset_idx=1, end_col_offset_idx=2),
    ]
    doc.add_table(data=TableData(num_rows=2, num_cols=2, table_cells=cells), prov=_prov(1))

    doc.add_picture(
        image=ImageRef.from_pil(Image.new("RGB", (8, 8), "red"), dpi=72),
        prov=_prov(2),
    )
    return doc


@pytest.fixture
def paths(tmp_path: Path) -> JobPaths:
    paths = JobPaths(tmp_path)
    paths.create("job-1")
    return paths


def test_writes_markdown(document, paths):
    manifest = write_artifacts(document, paths, "job-1")
    content = paths.markdown_file("job-1").read_text(encoding="utf-8")
    assert "Fixture Document" in content
    assert manifest["markdown"]["bytes"] == len(content.encode("utf-8"))


def test_writes_document_json(document, paths):
    manifest = write_artifacts(document, paths, "job-1")
    payload = json.loads(paths.document_file("job-1").read_text(encoding="utf-8"))
    assert payload["name"] == "fixture"
    assert manifest["document"]["bytes"] > 0


def test_page_count(document, paths):
    assert write_artifacts(document, paths, "job-1")["page_count"] == 2


def test_writes_tables_in_both_formats(document, paths):
    manifest = write_artifacts(document, paths, "job-1")
    assert len(manifest["tables"]) == 1

    entry = manifest["tables"][0]
    assert entry == {
        "index": 0, "rows": 2, "cols": 2, "page": 1,
        "csv": "table-000.csv", "html": "table-000.html",
    }
    assert "a" in paths.table_file("job-1", 0, "csv").read_text(encoding="utf-8")
    assert "<table" in paths.table_file("job-1", 0, "html").read_text(encoding="utf-8")


def test_writes_pictures(document, paths):
    manifest = write_artifacts(document, paths, "job-1")
    assert len(manifest["images"]) == 1

    entry = manifest["images"][0]
    assert entry["name"] == "picture-000.png"
    assert entry["kind"] == "picture"
    assert entry["page"] == 2
    assert entry["bytes"] > 0
    assert paths.image_file("job-1", "picture-000.png").exists()


def test_warnings_are_recorded(document, paths):
    manifest = write_artifacts(document, paths, "job-1", warnings=["page 2 was odd"])
    assert manifest["warnings"] == ["page 2 was odd"]


def test_document_without_pictures_still_succeeds(paths):
    doc = DoclingDocument(name="plain")
    doc.add_text(label="text", text="nothing but text")
    manifest = write_artifacts(doc, paths, "job-1")
    assert manifest["images"] == []
    assert manifest["tables"] == []
    assert manifest["page_count"] == 0
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_artifacts.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.conversion'`

- [ ] **Step 3: Implement `app/conversion/artifacts.py`**

`app/conversion/__init__.py` — empty file.

```python
import json
import logging

from app.storage.paths import JobPaths

logger = logging.getLogger(__name__)


def write_artifacts(
    document,
    paths: JobPaths,
    job_id: str,
    warnings: list[str] | None = None,
) -> dict:
    """Write every artifact for a converted document and return its manifest.

    Takes a `docling_core.types.doc.DoclingDocument`. Typed loosely so this
    module imports nothing from docling itself.
    """
    markdown_bytes = _write_markdown(document, paths, job_id)
    document_bytes = _write_document_json(document, paths, job_id)

    return {
        "page_count": document.num_pages(),
        "warnings": list(warnings or []),
        "markdown": {"bytes": markdown_bytes},
        "document": {"bytes": document_bytes},
        "tables": _write_tables(document, paths, job_id),
        "images": _write_pictures(document, paths, job_id),
    }


def _write_markdown(document, paths: JobPaths, job_id: str) -> int:
    target = paths.markdown_file(job_id)
    target.write_text(document.export_to_markdown(), encoding="utf-8")
    return target.stat().st_size


def _write_document_json(document, paths: JobPaths, job_id: str) -> int:
    target = paths.document_file(job_id)
    target.write_text(
        json.dumps(document.export_to_dict(), ensure_ascii=False), encoding="utf-8"
    )
    return target.stat().st_size


def _write_tables(document, paths: JobPaths, job_id: str) -> list[dict]:
    entries: list[dict] = []
    for index, table in enumerate(document.tables):
        csv_target = paths.table_file(job_id, index, "csv")
        html_target = paths.table_file(job_id, index, "html")

        frame = table.export_to_dataframe(doc=document)
        frame.to_csv(csv_target, index=False)
        html_target.write_text(table.export_to_html(doc=document), encoding="utf-8")

        entries.append(
            {
                "index": index,
                "rows": table.data.num_rows,
                "cols": table.data.num_cols,
                "page": _page_of(table),
                "csv": csv_target.name,
                "html": html_target.name,
            }
        )
    return entries


def _write_pictures(document, paths: JobPaths, job_id: str) -> list[dict]:
    entries: list[dict] = []
    for index, picture in enumerate(document.pictures):
        image = picture.get_image(document)
        if image is None:
            # The pipeline ran without generate_picture_images; nothing to save.
            logger.debug("Picture %d of job %s carries no image", index, job_id)
            continue

        name = f"picture-{index:03d}.png"
        target = paths.image_file(job_id, name)
        image.save(target, format="PNG")

        entries.append(
            {
                "name": name,
                "kind": "picture",
                "page": _page_of(picture),
                "bytes": target.stat().st_size,
            }
        )
    return entries


def _page_of(item) -> int | None:
    provenance = getattr(item, "prov", None)
    return provenance[0].page_no if provenance else None
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_artifacts.py -v`
Expected: PASS (8 tests)

> If `export_to_dataframe(doc=...)` or `export_to_html(doc=...)` raises a `TypeError` about the `doc` keyword, the installed `docling-core` predates that signature. Check the installed version with `.venv/bin/pip show docling-core`, and fix the pin in `requirements-base.txt` rather than dropping the argument.

- [ ] **Step 5: Commit**

```bash
git add app/conversion tests/test_artifacts.py
git commit -m "feat: write markdown, JSON, tables, and images as job artifacts"
```

---

### Task 7: Converter provider (the only docling importer)

**Files:**
- Create: `app/conversion/converters.py`
- Test: `tests/test_converters.py`

**Interfaces:**
- Consumes: `ConversionOptions` (Task 5), `Settings` (Task 1).
- Produces:
  - `app.conversion.converters.ConverterProvider(models_dir: Path | None, timeout_seconds: float | None, cache_size: int = 4)`.
  - `ConverterProvider.get(options: ConversionOptions)` — returns a docling `DocumentConverter`, cached per thread in an LRU keyed by `options.cache_key()`.
  - `ConverterProvider.build_pipeline_options(options: ConversionOptions)` — maps our options onto `PdfPipelineOptions`; separated so it is testable without constructing a converter.
  - `ConverterProvider.cached_key_count()` — number of entries cached for the calling thread (test hook).

All docling imports live inside methods.

- [ ] **Step 1: Write the failing test**

`tests/test_converters.py`:

```python
import threading
from pathlib import Path

import pytest

from app.api.schemas import ConversionOptions, TableMode
from app.conversion.converters import ConverterProvider

docling = pytest.importorskip("docling", reason="real docling not installed")


@pytest.fixture
def provider() -> ConverterProvider:
    return ConverterProvider(models_dir=None, timeout_seconds=90.0, cache_size=2)


def test_pipeline_options_map_ocr_off(provider):
    options = ConversionOptions(ocr=False, ocr_languages=["en"],
                                table_mode=TableMode.FAST, image_scale=1.0)
    pipeline = provider.build_pipeline_options(options)
    assert pipeline.do_ocr is False
    assert pipeline.do_table_structure is True
    assert pipeline.table_structure_options.mode.value == "fast"
    assert pipeline.images_scale == 1.0
    assert pipeline.generate_picture_images is True
    assert pipeline.generate_page_images is False
    assert pipeline.document_timeout == 90.0


def test_pipeline_options_map_ocr_on(provider):
    options = ConversionOptions(ocr=True, ocr_languages=["de", "en"],
                                table_mode=TableMode.ACCURATE, image_scale=2.0)
    pipeline = provider.build_pipeline_options(options)
    assert pipeline.do_ocr is True
    assert pipeline.ocr_options.lang == ["de", "en"]
    assert pipeline.table_structure_options.mode.value == "accurate"


def test_models_dir_becomes_artifacts_path(tmp_path: Path):
    provider = ConverterProvider(models_dir=tmp_path, timeout_seconds=None)
    pipeline = provider.build_pipeline_options(ConversionOptions())
    assert Path(pipeline.artifacts_path) == tmp_path
    assert pipeline.document_timeout is None


def test_same_options_reuse_one_converter(provider):
    options = ConversionOptions()
    assert provider.get(options) is provider.get(options)
    assert provider.cached_key_count() == 1


def test_different_options_get_different_converters(provider):
    first = provider.get(ConversionOptions(table_mode=TableMode.FAST))
    second = provider.get(ConversionOptions(table_mode=TableMode.ACCURATE))
    assert first is not second
    assert provider.cached_key_count() == 2


def test_cache_evicts_beyond_its_size(provider):
    provider.get(ConversionOptions(image_scale=1.0))
    provider.get(ConversionOptions(image_scale=1.5))
    provider.get(ConversionOptions(image_scale=2.0))
    assert provider.cached_key_count() == 2


def test_cache_is_per_thread(provider):
    provider.get(ConversionOptions())
    counts: list[int] = []

    def in_other_thread() -> None:
        counts.append(provider.cached_key_count())

    thread = threading.Thread(target=in_other_thread)
    thread.start()
    thread.join()

    assert counts == [0]
```

> `pytest.importorskip("docling")` keeps this file out of the fast suite when torch is not installed. Run it with real docling present — inside the built image is fine: `docker compose run --rm api pytest tests/test_converters.py -v`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_converters.py -v`
Expected: SKIPPED if docling is not installed locally, otherwise FAIL — `ModuleNotFoundError: No module named 'app.conversion.converters'`

- [ ] **Step 3: Implement `app/conversion/converters.py`**

```python
import logging
import threading
from collections import OrderedDict
from pathlib import Path

from app.api.schemas import ConversionOptions, TableMode

logger = logging.getLogger(__name__)


class ConverterProvider:
    """Supplies docling converters, cached per thread in a small LRU.

    Every docling import happens inside a method, so importing this module
    costs nothing and the fast test suite runs without torch installed. Each
    cached converter holds live model pipelines, so the cache is deliberately
    small.
    """

    def __init__(
        self,
        models_dir: Path | None,
        timeout_seconds: float | None,
        cache_size: int = 4,
    ) -> None:
        self._models_dir = models_dir
        self._timeout_seconds = timeout_seconds
        self._cache_size = max(1, cache_size)
        self._local = threading.local()

    def build_pipeline_options(self, options: ConversionOptions):
        from docling.datamodel.pipeline_options import (
            EasyOcrOptions,
            PdfPipelineOptions,
            TableFormerMode,
            TableStructureOptions,
        )

        mode = TableFormerMode.FAST if options.table_mode is TableMode.FAST else TableFormerMode.ACCURATE

        pipeline_options = PdfPipelineOptions(
            do_ocr=options.ocr,
            do_table_structure=True,
            table_structure_options=TableStructureOptions(do_cell_matching=True, mode=mode),
            # EasyOCR is pinned rather than docling's OcrAutoOptions so behavior
            # is deterministic and the offline container never probes for an
            # engine it does not ship.
            ocr_options=EasyOcrOptions(
                lang=list(options.ocr_languages),
                download_enabled=self._models_dir is None,
            ),
            images_scale=options.image_scale,
            generate_picture_images=True,
            generate_page_images=False,
            document_timeout=self._timeout_seconds,
        )
        if self._models_dir is not None:
            pipeline_options.artifacts_path = self._models_dir
        return pipeline_options

    def get(self, options: ConversionOptions):
        """Return a converter for these options, reusing one where possible."""
        cache = self._cache()
        key = options.cache_key()

        if key in cache:
            cache.move_to_end(key)
            return cache[key]

        converter = self._build_converter(options)
        cache[key] = converter
        while len(cache) > self._cache_size:
            evicted, _ = cache.popitem(last=False)
            logger.info("Evicted cached converter for options %s", evicted)
        return converter

    def cached_key_count(self) -> int:
        return len(self._cache())

    def _cache(self) -> OrderedDict:
        cache = getattr(self._local, "cache", None)
        if cache is None:
            cache = OrderedDict()
            self._local.cache = cache
        return cache

    def _build_converter(self, options: ConversionOptions):
        from docling.datamodel.base_models import InputFormat
        from docling.document_converter import DocumentConverter, PdfFormatOption

        logger.info("Building docling converter for options %s", options.cache_key())
        return DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(
                    pipeline_options=self.build_pipeline_options(options)
                )
            }
        )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_converters.py -v`
Expected: PASS (7 tests) where docling is installed; SKIPPED otherwise. It must pass inside the container in Task 12.

- [ ] **Step 5: Commit**

```bash
git add app/conversion/converters.py tests/test_converters.py
git commit -m "feat: add per-thread docling converter provider with bounded cache"
```

---

### Task 8: Conversion service

**Files:**
- Create: `app/conversion/service.py`
- Test: `tests/test_service.py`

**Interfaces:**
- Consumes: `JobPaths` (Task 3), `ConversionOptions` (Task 5), `write_artifacts` (Task 6), `ConverterProvider` (Task 7).
- Produces:
  - `app.conversion.service.ConversionFailed(error_type: str, message: str)` — exception with `.error_type` and `.message`.
  - `app.conversion.service.ConversionService(paths: JobPaths, converter_provider)` with `run(job_id: str, input_path: Path, options: ConversionOptions) -> dict` returning the manifest.

`run()` is called from a worker thread and is synchronous. Status handling: `SUCCESS` → write artifacts; `PARTIAL_SUCCESS` with timeout errors → `ConversionFailed("timeout", ...)`; `PARTIAL_SUCCESS` otherwise → write artifacts with the messages as `warnings`; anything else → `ConversionFailed("conversion_failed", ...)`.

The converter provider is injected, so these tests use a stub and never load a model.

- [ ] **Step 1: Write the failing test**

`tests/test_service.py`:

```python
from pathlib import Path

import pytest
from docling_core.types.doc import DoclingDocument

from app.api.schemas import ConversionOptions
from app.conversion.service import ConversionFailed, ConversionService
from app.storage.paths import JobPaths


class FakeResult:
    def __init__(self, status: str, document=None, errors=(), timed_out=False):
        self.status = FakeStatus(status)
        self.document = document
        self.errors = list(errors)
        self._timed_out = timed_out

    def has_timeout_errors(self) -> bool:
        return self._timed_out


class FakeStatus:
    """Mimics docling's ConversionStatus enum closely enough to compare by value."""

    def __init__(self, value: str):
        self.value = value


class FakeError:
    def __init__(self, message: str):
        self.error_message = message


class FakeConverter:
    def __init__(self, result):
        self.result = result
        self.calls: list[Path] = []

    def convert(self, source):
        self.calls.append(source)
        return self.result


class FakeProvider:
    def __init__(self, converter):
        self.converter = converter
        self.options_seen: list[ConversionOptions] = []

    def get(self, options):
        self.options_seen.append(options)
        return self.converter


@pytest.fixture
def paths(tmp_path: Path) -> JobPaths:
    paths = JobPaths(tmp_path)
    paths.create("job-1")
    return paths


def _document() -> DoclingDocument:
    doc = DoclingDocument(name="fixture")
    doc.add_text(label="text", text="hello")
    return doc


def test_success_writes_artifacts_and_returns_manifest(paths):
    converter = FakeConverter(FakeResult("success", document=_document()))
    service = ConversionService(paths, FakeProvider(converter))

    manifest = service.run("job-1", Path("/tmp/in.pdf"), ConversionOptions())

    assert manifest["warnings"] == []
    assert paths.markdown_file("job-1").read_text(encoding="utf-8").strip() == "hello"
    assert converter.calls == [Path("/tmp/in.pdf")]


def test_options_are_passed_to_the_provider(paths):
    provider = FakeProvider(FakeConverter(FakeResult("success", document=_document())))
    options = ConversionOptions(ocr=False)
    ConversionService(paths, provider).run("job-1", Path("/tmp/in.pdf"), options)
    assert provider.options_seen == [options]


def test_partial_success_records_warnings(paths):
    result = FakeResult(
        "partial_success", document=_document(), errors=[FakeError("page 3 was skipped")]
    )
    service = ConversionService(paths, FakeProvider(FakeConverter(result)))

    manifest = service.run("job-1", Path("/tmp/in.pdf"), ConversionOptions())

    assert manifest["warnings"] == ["page 3 was skipped"]


def test_timeout_raises_conversion_failed(paths):
    result = FakeResult(
        "partial_success", document=_document(),
        errors=[FakeError("timed out")], timed_out=True,
    )
    service = ConversionService(paths, FakeProvider(FakeConverter(result)))

    with pytest.raises(ConversionFailed) as excinfo:
        service.run("job-1", Path("/tmp/in.pdf"), ConversionOptions())

    assert excinfo.value.error_type == "timeout"


def test_failure_status_raises_conversion_failed(paths):
    result = FakeResult("failure", errors=[FakeError("unsupported format")])
    service = ConversionService(paths, FakeProvider(FakeConverter(result)))

    with pytest.raises(ConversionFailed) as excinfo:
        service.run("job-1", Path("/tmp/in.pdf"), ConversionOptions())

    assert excinfo.value.error_type == "conversion_failed"
    assert "unsupported format" in excinfo.value.message


def test_missing_document_raises_conversion_failed(paths):
    service = ConversionService(paths, FakeProvider(FakeConverter(FakeResult("success"))))
    with pytest.raises(ConversionFailed):
        service.run("job-1", Path("/tmp/in.pdf"), ConversionOptions())
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.conversion.service'`

- [ ] **Step 3: Implement `app/conversion/service.py`**

```python
import logging
from pathlib import Path

from app.api.schemas import ConversionOptions
from app.conversion.artifacts import write_artifacts
from app.storage.paths import JobPaths

logger = logging.getLogger(__name__)

_SUCCESS = "success"
_PARTIAL_SUCCESS = "partial_success"


class ConversionFailed(Exception):
    """A conversion that could not produce a usable document."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message


class ConversionService:
    """Runs one document through docling and writes its artifacts."""

    def __init__(self, paths: JobPaths, converter_provider) -> None:
        self._paths = paths
        self._converter_provider = converter_provider

    def run(self, job_id: str, input_path: Path, options: ConversionOptions) -> dict:
        """Convert the document and return its artifact manifest.

        Synchronous; called from a worker thread. Raises ConversionFailed for
        anything the caller should record as a failed job.
        """
        converter = self._converter_provider.get(options)
        result = converter.convert(input_path)
        status = result.status.value
        messages = [error.error_message for error in getattr(result, "errors", [])]

        if status == _PARTIAL_SUCCESS and result.has_timeout_errors():
            raise ConversionFailed(
                "timeout", _join(messages) or "Conversion exceeded its time limit."
            )

        if status not in (_SUCCESS, _PARTIAL_SUCCESS):
            raise ConversionFailed(
                "conversion_failed",
                _join(messages) or f"docling reported status {status!r}.",
            )

        if result.document is None:
            raise ConversionFailed(
                "conversion_failed", "docling returned no document for this input."
            )

        warnings = messages if status == _PARTIAL_SUCCESS else []
        if warnings:
            logger.warning("Job %s converted with warnings: %s", job_id, _join(warnings))

        return write_artifacts(result.document, self._paths, job_id, warnings=warnings)


def _join(messages: list[str]) -> str:
    return "; ".join(message for message in messages if message)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_service.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add app/conversion/service.py tests/test_service.py
git commit -m "feat: add conversion service with timeout and partial-success handling"
```

---

### Task 9: Worker pool

**Files:**
- Create: `app/jobs/pool.py`
- Test: `tests/test_pool.py`

**Interfaces:**
- Consumes: `JobStore` (Task 4), `JobPaths` (Task 3), `ConversionService`/`ConversionFailed` (Task 8), `ConversionOptions` (Task 5).
- Produces:
  - `app.jobs.pool.QueueFull` — exception raised by `enqueue` when the queue is at capacity.
  - `app.jobs.pool.WorkerPool(store, service, paths, threads: int, queue_max: int)` with `start()`, `stop(grace_seconds: float)`, `enqueue(job_id: str) -> None`, `queue_depth` property, `worker_count` property, and `wait_idle(timeout: float) -> bool` (test hook: blocks until the queue is drained and no job is in flight).

A worker resolves the job from the store, reads its options and input file, calls the service, and records the outcome. A `ConversionFailed` becomes `mark_failed(type, message)`; any other exception becomes `mark_failed("internal_error", ...)` and is logged with a traceback. The loop body catches `Exception` so a worker thread cannot die — this is the supervision mechanism.

- [ ] **Step 1: Write the failing test**

`tests/test_pool.py`:

```python
import threading
from pathlib import Path

import pytest

from app.conversion.service import ConversionFailed
from app.jobs.models import JobStatus
from app.jobs.pool import QueueFull, WorkerPool
from app.jobs.store import JobStore
from app.storage.paths import JobPaths


class RecordingService:
    """Stands in for ConversionService; records calls, never loads a model."""

    def __init__(self, manifest=None, raises=None):
        self.manifest = manifest if manifest is not None else {"page_count": 1}
        self.raises = raises
        self.calls: list[tuple] = []
        self.released = threading.Event()
        self.released.set()

    def run(self, job_id, input_path, options):
        self.released.wait(timeout=5)
        self.calls.append((job_id, input_path, options))
        if self.raises is not None:
            raise self.raises
        return self.manifest


@pytest.fixture
def harness(tmp_path: Path):
    paths = JobPaths(tmp_path)
    store = JobStore(tmp_path / "jobs.sqlite3", paths)
    store.connect()
    yield store, paths
    store.close()


def _seed_job(store: JobStore, paths: JobPaths, job_id: str) -> None:
    paths.create(job_id)
    paths.input_file(job_id, "in.pdf").write_bytes(b"%PDF-1.4")
    store.create(job_id, "in.pdf", {"ocr": True, "ocr_languages": ["en"],
                                    "table_mode": "accurate", "image_scale": 2.0})


def test_successful_job_is_marked_succeeded(harness):
    store, paths = harness
    service = RecordingService(manifest={"page_count": 7})
    pool = WorkerPool(store, service, paths, threads=1, queue_max=4)
    pool.start()
    try:
        _seed_job(store, paths, "job-1")
        pool.enqueue("job-1")
        assert pool.wait_idle(timeout=5)
    finally:
        pool.stop(grace_seconds=5)

    job = store.get("job-1")
    assert job.status is JobStatus.SUCCEEDED
    assert job.manifest == {"page_count": 7}
    assert job.started_at is not None


def test_service_receives_input_path_and_options(harness):
    store, paths = harness
    service = RecordingService()
    pool = WorkerPool(store, service, paths, threads=1, queue_max=4)
    pool.start()
    try:
        _seed_job(store, paths, "job-1")
        pool.enqueue("job-1")
        assert pool.wait_idle(timeout=5)
    finally:
        pool.stop(grace_seconds=5)

    job_id, input_path, options = service.calls[0]
    assert job_id == "job-1"
    assert input_path == paths.input_file("job-1", "in.pdf")
    assert options.ocr is True


def test_conversion_failure_is_recorded(harness):
    store, paths = harness
    service = RecordingService(raises=ConversionFailed("timeout", "too slow"))
    pool = WorkerPool(store, service, paths, threads=1, queue_max=4)
    pool.start()
    try:
        _seed_job(store, paths, "job-1")
        pool.enqueue("job-1")
        assert pool.wait_idle(timeout=5)
    finally:
        pool.stop(grace_seconds=5)

    job = store.get("job-1")
    assert job.status is JobStatus.FAILED
    assert job.error_type == "timeout"
    assert job.error_message == "too slow"


def test_unexpected_error_becomes_internal_error(harness):
    store, paths = harness
    service = RecordingService(raises=RuntimeError("boom"))
    pool = WorkerPool(store, service, paths, threads=1, queue_max=4)
    pool.start()
    try:
        _seed_job(store, paths, "job-1")
        pool.enqueue("job-1")
        assert pool.wait_idle(timeout=5)
    finally:
        pool.stop(grace_seconds=5)

    job = store.get("job-1")
    assert job.status is JobStatus.FAILED
    assert job.error_type == "internal_error"


def test_worker_survives_a_bad_job(harness):
    store, paths = harness
    service = RecordingService(raises=RuntimeError("boom"))
    pool = WorkerPool(store, service, paths, threads=1, queue_max=4)
    pool.start()
    try:
        _seed_job(store, paths, "job-1")
        _seed_job(store, paths, "job-2")
        pool.enqueue("job-1")
        pool.enqueue("job-2")
        assert pool.wait_idle(timeout=5)
        assert pool.worker_count == 1
    finally:
        pool.stop(grace_seconds=5)

    assert store.get("job-2").status is JobStatus.FAILED
    assert len(service.calls) == 2


def test_enqueue_raises_when_the_queue_is_full(harness):
    store, paths = harness
    service = RecordingService()
    service.released.clear()  # hold the worker so the queue backs up
    pool = WorkerPool(store, service, paths, threads=1, queue_max=1)
    pool.start()
    try:
        for job_id in ("job-1", "job-2", "job-3"):
            _seed_job(store, paths, job_id)
        pool.enqueue("job-1")
        pool.enqueue("job-2")
        with pytest.raises(QueueFull):
            pool.enqueue("job-3")
        assert pool.queue_depth >= 1
    finally:
        service.released.set()
        pool.stop(grace_seconds=5)


def test_missing_job_row_is_skipped(harness):
    store, paths = harness
    service = RecordingService()
    pool = WorkerPool(store, service, paths, threads=1, queue_max=4)
    pool.start()
    try:
        pool.enqueue("ghost")
        assert pool.wait_idle(timeout=5)
    finally:
        pool.stop(grace_seconds=5)

    assert service.calls == []


def test_missing_input_file_fails_the_job(harness):
    store, paths = harness
    service = RecordingService()
    pool = WorkerPool(store, service, paths, threads=1, queue_max=4)
    pool.start()
    try:
        paths.create("job-1")
        store.create("job-1", "in.pdf", {})
        pool.enqueue("job-1")
        assert pool.wait_idle(timeout=5)
    finally:
        pool.stop(grace_seconds=5)

    job = store.get("job-1")
    assert job.status is JobStatus.FAILED
    assert job.error_type == "input_missing"
    assert service.calls == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_pool.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.jobs.pool'`

- [ ] **Step 3: Implement `app/jobs/pool.py`**

```python
import logging
import queue
import threading
import time

from app.api.schemas import ConversionOptions
from app.conversion.service import ConversionFailed
from app.jobs.store import JobStore
from app.storage.paths import JobPaths

logger = logging.getLogger(__name__)

_SHUTDOWN = object()


class QueueFull(Exception):
    """The conversion queue is at capacity."""


class WorkerPool:
    """A bounded work queue drained by a fixed set of conversion threads."""

    def __init__(
        self,
        store: JobStore,
        service,
        paths: JobPaths,
        threads: int,
        queue_max: int,
    ) -> None:
        self._store = store
        self._service = service
        self._paths = paths
        self._thread_count = max(1, threads)
        # queue.Queue, not asyncio.Queue: the consumers are OS threads.
        self._queue: queue.Queue = queue.Queue(maxsize=max(1, queue_max))
        self._threads: list[threading.Thread] = []
        self._in_flight = 0
        self._in_flight_lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()

    def start(self) -> None:
        for index in range(self._thread_count):
            thread = threading.Thread(
                target=self._run, name=f"docling-worker-{index}", daemon=True
            )
            thread.start()
            self._threads.append(thread)
        logger.info("Started %d conversion worker(s)", self._thread_count)

    def stop(self, grace_seconds: float) -> None:
        for _ in self._threads:
            self._queue.put(_SHUTDOWN)
        deadline = time.monotonic() + grace_seconds
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        self._threads.clear()

    def enqueue(self, job_id: str) -> None:
        try:
            self._queue.put_nowait(job_id)
        except queue.Full as exc:
            raise QueueFull(job_id) from exc
        self._idle.clear()

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    @property
    def worker_count(self) -> int:
        return sum(1 for thread in self._threads if thread.is_alive())

    def wait_idle(self, timeout: float) -> bool:
        """Block until the queue is drained and nothing is in flight."""
        return self._idle.wait(timeout=timeout)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _SHUTDOWN:
                self._queue.task_done()
                return

            self._enter_job()
            try:
                self._process(item)
            except Exception:
                # Nothing may escape: a worker thread must outlive a bad job.
                logger.exception("Worker loop error on job %s", item)
            finally:
                self._queue.task_done()
                self._leave_job()

    def _process(self, job_id: str) -> None:
        job = self._store.get(job_id)
        if job is None:
            logger.warning("Job %s vanished before it could run", job_id)
            return

        input_path = self._paths.input_file(job_id, job.filename)
        if not input_path.exists():
            self._store.mark_failed(
                job_id, "input_missing", "The uploaded file is no longer on disk."
            )
            return

        self._store.mark_running(job_id)
        started = time.monotonic()
        try:
            manifest = self._service.run(job_id, input_path, ConversionOptions(**job.options))
        except ConversionFailed as exc:
            logger.info("Job %s failed (%s): %s", job_id, exc.error_type, exc.message)
            self._store.mark_failed(job_id, exc.error_type, exc.message)
        except Exception as exc:
            logger.exception("Job %s raised an unexpected error", job_id)
            self._store.mark_failed(job_id, "internal_error", f"{type(exc).__name__}: {exc}")
        else:
            self._store.mark_succeeded(job_id, manifest)
            logger.info(
                "Job %s succeeded in %.1fs (%s pages)",
                job_id,
                time.monotonic() - started,
                manifest.get("page_count"),
            )

    def _enter_job(self) -> None:
        with self._in_flight_lock:
            self._in_flight += 1
            self._idle.clear()

    def _leave_job(self) -> None:
        with self._in_flight_lock:
            self._in_flight -= 1
            if self._in_flight == 0 and self._queue.empty():
                self._idle.set()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_pool.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add app/jobs/pool.py tests/test_pool.py
git commit -m "feat: add bounded worker pool with backpressure and supervision"
```

---

### Task 10: App wiring, lifespan, and full health

**Files:**
- Modify: `app/main.py`, `app/api/deps.py`
- Create: `tests/conftest.py`
- Test: `tests/test_lifespan.py`
- Modify: `tests/test_health.py`

**Interfaces:**
- Consumes: everything from Tasks 1–9.
- Produces:
  - `app.main.create_app(settings=None, service=None)` — `service` overrides the real `ConversionService`, which is how every later test avoids loading models.
  - App state: `app.state.settings`, `app.state.paths`, `app.state.store`, `app.state.pool`.
  - `app.api.deps.get_store(request)`, `get_pool(request)`, `get_paths(request)`.
  - `/health` returns `{"status", "models_loaded", "workers", "queue_depth"}`.
  - `tests/conftest.py` fixtures: `settings`, `recording_service`, `client`.

`models_loaded` reports whether real docling is wired in (false when a service was injected for tests).

- [ ] **Step 1: Write the failing tests**

`tests/conftest.py`:

```python
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

API_KEY = "test-key"


class RecordingService:
    """A ConversionService stand-in that writes canned artifacts instantly."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.raises: Exception | None = None
        self.manifest: dict | None = None

    def run(self, job_id, input_path, options):
        self.calls.append((job_id, input_path, options))
        if self.raises is not None:
            raise self.raises
        return self.manifest if self.manifest is not None else {
            "page_count": 1,
            "warnings": [],
            "markdown": {"bytes": 5},
            "document": {"bytes": 2},
            "tables": [],
            "images": [],
        }


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(api_keys=API_KEY, data_dir=tmp_path, worker_threads=1, queue_max=4)


@pytest.fixture
def recording_service() -> RecordingService:
    return RecordingService()


@pytest.fixture
def client(settings: Settings, recording_service: RecordingService):
    app = create_app(settings=settings, service=recording_service)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth() -> dict:
    return {"X-API-Key": API_KEY}
```

`tests/test_lifespan.py`:

```python
from app.config import Settings
from app.jobs.models import JobStatus
from app.jobs.store import JobStore
from app.main import create_app
from app.storage.paths import JobPaths
from fastapi.testclient import TestClient


def test_health_reports_pool_state(client):
    payload = client.get("/health").json()
    assert payload["status"] == "ok"
    assert payload["workers"] == 1
    assert payload["queue_depth"] == 0
    assert payload["models_loaded"] is False


def test_startup_fails_interrupted_jobs(settings: Settings, recording_service):
    paths = JobPaths(settings.data_dir)
    seed = JobStore(settings.db_path, paths)
    seed.connect()
    seed.create("stuck", "a.pdf", {})
    seed.mark_running("stuck")
    seed.close()

    app = create_app(settings=settings, service=recording_service)
    with TestClient(app):
        pass

    check = JobStore(settings.db_path, paths)
    check.connect()
    job = check.get("stuck")
    check.close()

    assert job.status is JobStatus.FAILED
    assert job.error_type == "interrupted"


def test_startup_requeues_queued_jobs(settings: Settings, recording_service):
    paths = JobPaths(settings.data_dir)
    paths.create("pending")
    paths.input_file("pending", "a.pdf").write_bytes(b"%PDF-1.4")

    seed = JobStore(settings.db_path, paths)
    seed.connect()
    seed.create("pending", "a.pdf", {})
    seed.close()

    app = create_app(settings=settings, service=recording_service)
    with TestClient(app) as test_client:
        assert test_client.app.state.pool.wait_idle(timeout=5)

    assert recording_service.calls[0][0] == "pending"
```

Replace the body of `tests/test_health.py` with:

```python
def test_health_is_unauthenticated_and_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_unknown_route_uses_error_envelope(client):
    response = client.get("/nope")
    assert response.status_code == 404
    assert response.json() == {"error": {"code": "not_found", "message": "Not Found"}}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_lifespan.py -v`
Expected: FAIL — `TypeError: create_app() got an unexpected keyword argument 'service'`

- [ ] **Step 3: Add the state accessors to `app/api/deps.py`**

Append:

```python
def get_store(request: Request):
    return request.app.state.store


def get_pool(request: Request):
    return request.app.state.pool


def get_paths(request: Request):
    return request.app.state.paths
```

- [ ] **Step 4: Rewrite `app/main.py`**

```python
import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request

from app.api.errors import install_error_handlers
from app.config import Settings, get_settings
from app.conversion.converters import ConverterProvider
from app.conversion.service import ConversionService
from app.jobs.pool import QueueFull, WorkerPool
from app.jobs.store import JobStore
from app.storage.paths import JobPaths

logger = logging.getLogger(__name__)

health_router = APIRouter()


@health_router.get("/health", tags=["health"], summary="Liveness and readiness probe")
async def health(request: Request) -> dict:
    pool = request.app.state.pool
    return {
        "status": "ok",
        "models_loaded": request.app.state.models_loaded,
        "workers": pool.worker_count,
        "queue_depth": pool.queue_depth,
    }


def create_app(settings: Settings | None = None, service=None) -> FastAPI:
    """Build the application. Pass `service` to substitute the docling layer."""
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    paths = JobPaths(settings.data_dir)
    store = JobStore(settings.db_path, paths)

    models_loaded = service is None
    if service is None:
        service = ConversionService(
            paths,
            ConverterProvider(
                models_dir=settings.models_dir,
                timeout_seconds=settings.job_timeout_seconds,
                cache_size=settings.converter_cache_size,
            ),
        )

    pool = WorkerPool(
        store=store,
        service=service,
        paths=paths,
        threads=settings.worker_threads,
        queue_max=settings.queue_max,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        store.connect()
        pool.start()

        for job_id in store.recover_interrupted():
            try:
                pool.enqueue(job_id)
                logger.info("Re-queued job %s after restart", job_id)
            except QueueFull:
                store.mark_failed(
                    job_id,
                    "interrupted",
                    "The queue was full when the service restarted.",
                )
        yield

        pool.stop(grace_seconds=settings.shutdown_grace_seconds)
        store.close()

    app = FastAPI(
        title="Docling Extraction API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    app.state.settings = settings
    app.state.paths = paths
    app.state.store = store
    app.state.pool = pool
    app.state.models_loaded = models_loaded

    install_error_handlers(app)
    app.include_router(health_router)
    return app
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_lifespan.py tests/test_health.py tests/test_auth.py -v`
Expected: PASS

> `tests/test_auth.py` builds its own app via `create_app(Settings(...))` without a service; that path constructs a `ConverterProvider` but never converts anything, so no model is loaded. If it now fails on an import of docling, a module-level docling import has crept in — fix that rather than the test.

- [ ] **Step 6: Run the whole fast suite**

Run: `.venv/bin/pytest -v`
Expected: PASS, no test loading a model.

- [ ] **Step 7: Commit**

```bash
git add app/main.py app/api/deps.py tests/conftest.py tests/test_lifespan.py tests/test_health.py
git commit -m "feat: wire store, pool, and conversion service into the app lifespan"
```

---

### Task 11: Upload and job status routes

**Files:**
- Create: `app/api/routes.py`
- Modify: `app/main.py`, `app/api/schemas.py`
- Test: `tests/test_routes_documents.py`

**Interfaces:**
- Consumes: everything from Tasks 1–10.
- Produces:
  - `app.api.routes.router` — `APIRouter(prefix="/v1", tags=["documents"])`, included by `create_app`.
  - `POST /v1/documents` → 202 `JobCreated`; 503 on `QueueFull`; 400 on bad options.
  - `GET /v1/documents/{job_id}` → 200 `JobDetail`; 404 unknown.
  - New response models in `app/api/schemas.py`: `JobCreated`, `JobLinks`, `JobError`, `ArtifactRef`, `TableRef`, `ImageRef`, `JobResult`, `JobDetail`, `ErrorEnvelope`, `ErrorBody`.
  - `app.api.schemas.build_job_detail(job: Job, base: str) -> JobDetail` — turns a stored job plus its manifest into the response, generating artifact URLs from manifest filenames.

- [ ] **Step 1: Write the failing test**

`tests/test_routes_documents.py`:

```python
import json

from app.conversion.service import ConversionFailed


def _upload(client, auth, *, content=b"%PDF-1.4 fake", filename="report.pdf", options=None):
    data = {"options": json.dumps(options)} if options is not None else None
    return client.post(
        "/v1/documents",
        headers=auth,
        files={"file": (filename, content, "application/pdf")},
        data=data,
    )


def test_upload_returns_202_with_an_id(client, auth):
    response = _upload(client, auth)
    assert response.status_code == 202

    payload = response.json()
    assert payload["status"] == "queued"
    assert payload["filename"] == "report.pdf"
    assert payload["created_at"]
    assert payload["links"]["self"] == f"/v1/documents/{payload['id']}"
    assert len(payload["id"]) == 36


def test_upload_requires_a_key(client):
    response = client.post(
        "/v1/documents", files={"file": ("a.pdf", b"x", "application/pdf")}
    )
    assert response.status_code == 401


def test_upload_stores_the_file(client, auth, settings):
    job_id = _upload(client, auth, content=b"%PDF-1.4 payload").json()["id"]
    from app.storage.paths import JobPaths

    stored = JobPaths(settings.data_dir).input_file(job_id, "report.pdf")
    assert stored.read_bytes() == b"%PDF-1.4 payload"


def test_upload_sanitizes_the_filename(client, auth, settings):
    response = _upload(client, auth, filename="../../evil.pdf")
    assert response.json()["filename"] == "evil.pdf"


def test_upload_rejects_unknown_options(client, auth):
    response = _upload(client, auth, options={"colour": "blue"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_upload_applies_options(client, auth, recording_service):
    _upload(client, auth, options={"ocr": False, "table_mode": "fast"})
    assert client.app.state.pool.wait_idle(timeout=5)

    _, _, options = recording_service.calls[0]
    assert options.ocr is False
    assert options.table_mode.value == "fast"


def test_job_reaches_succeeded(client, auth):
    job_id = _upload(client, auth).json()["id"]
    assert client.app.state.pool.wait_idle(timeout=5)

    payload = client.get(f"/v1/documents/{job_id}", headers=auth).json()
    assert payload["status"] == "succeeded"
    assert payload["error"] is None
    assert payload["finished_at"] is not None


def test_succeeded_job_exposes_artifact_urls(client, auth, recording_service):
    recording_service.manifest = {
        "page_count": 12,
        "warnings": [],
        "markdown": {"bytes": 4821},
        "document": {"bytes": 91234},
        "tables": [{"index": 0, "rows": 8, "cols": 4, "page": 3,
                    "csv": "table-000.csv", "html": "table-000.html"}],
        "images": [{"name": "picture-000.png", "kind": "picture", "page": 5, "bytes": 44210}],
    }
    job_id = _upload(client, auth).json()["id"]
    assert client.app.state.pool.wait_idle(timeout=5)

    result = client.get(f"/v1/documents/{job_id}", headers=auth).json()["result"]

    assert result["page_count"] == 12
    assert result["markdown"] == {"url": f"/v1/documents/{job_id}/markdown", "bytes": 4821}
    assert result["document"] == {
        "url": f"/v1/documents/{job_id}/document.json", "bytes": 91234
    }
    assert result["tables"][0]["csv_url"] == f"/v1/documents/{job_id}/tables/0?format=csv"
    assert result["tables"][0]["html_url"] == f"/v1/documents/{job_id}/tables/0?format=html"
    assert result["images"][0]["url"] == f"/v1/documents/{job_id}/images/picture-000.png"


def test_failed_job_reports_the_error(client, auth, recording_service):
    recording_service.raises = ConversionFailed("timeout", "took too long")
    job_id = _upload(client, auth).json()["id"]
    assert client.app.state.pool.wait_idle(timeout=5)

    payload = client.get(f"/v1/documents/{job_id}", headers=auth).json()
    assert payload["status"] == "failed"
    assert payload["result"] is None
    assert payload["error"] == {"type": "timeout", "message": "took too long"}


def test_unknown_job_is_404(client, auth):
    response = client.get("/v1/documents/does-not-exist", headers=auth)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_queue_full_returns_503(client, auth, recording_service, monkeypatch):
    from app.jobs.pool import QueueFull

    def always_full(_job_id: str) -> None:
        raise QueueFull(_job_id)

    monkeypatch.setattr(client.app.state.pool, "enqueue", always_full)

    response = _upload(client, auth)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "queue_full"
    assert response.headers["Retry-After"] == "30"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_routes_documents.py -v`
Expected: FAIL — all requests 404, because `/v1/documents` does not exist yet.

- [ ] **Step 3: Add response models to `app/api/schemas.py`**

Append:

```python
from typing import Literal

from app.jobs.models import Job, JobStatus


class JobLinks(BaseModel):
    self: str = Field(description="Canonical URL for polling this job.")


class JobCreated(BaseModel):
    id: str
    status: JobStatus
    filename: str
    created_at: str
    links: JobLinks


class JobError(BaseModel):
    type: str = Field(description="Machine-readable failure kind, e.g. 'timeout'.")
    message: str


class ArtifactRef(BaseModel):
    url: str
    bytes: int


class TableRef(BaseModel):
    index: int
    rows: int
    cols: int
    page: int | None = None
    csv_url: str
    html_url: str


class ImageRef(BaseModel):
    name: str
    kind: str
    page: int | None = None
    url: str
    bytes: int


class JobResult(BaseModel):
    page_count: int
    warnings: list[str] = []
    markdown: ArtifactRef
    document: ArtifactRef
    tables: list[TableRef] = []
    images: list[ImageRef] = []


class JobDetail(BaseModel):
    id: str
    status: JobStatus
    filename: str
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    options: ConversionOptions
    error: JobError | None = None
    result: JobResult | None = None


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorEnvelope(BaseModel):
    error: ErrorBody


def build_job_detail(job: Job, base: str) -> JobDetail:
    """Render a stored job as its API representation, expanding artifact URLs."""
    return JobDetail(
        id=job.id,
        status=job.status,
        filename=job.filename,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        options=ConversionOptions(**job.options) if job.options else ConversionOptions(),
        error=(
            JobError(type=job.error_type, message=job.error_message or "")
            if job.error_type
            else None
        ),
        result=_build_result(job, base) if job.manifest else None,
    )


def _build_result(job: Job, base: str) -> JobResult:
    manifest = job.manifest or {}
    return JobResult(
        page_count=manifest.get("page_count", 0),
        warnings=manifest.get("warnings", []),
        markdown=ArtifactRef(
            url=f"{base}/markdown", bytes=manifest.get("markdown", {}).get("bytes", 0)
        ),
        document=ArtifactRef(
            url=f"{base}/document.json", bytes=manifest.get("document", {}).get("bytes", 0)
        ),
        tables=[
            TableRef(
                index=entry["index"],
                rows=entry["rows"],
                cols=entry["cols"],
                page=entry.get("page"),
                csv_url=f"{base}/tables/{entry['index']}?format=csv",
                html_url=f"{base}/tables/{entry['index']}?format=html",
            )
            for entry in manifest.get("tables", [])
        ],
        images=[
            ImageRef(
                name=entry["name"],
                kind=entry["kind"],
                page=entry.get("page"),
                url=f"{base}/images/{entry['name']}",
                bytes=entry["bytes"],
            )
            for entry in manifest.get("images", [])
        ],
    )
```

> `Literal` is imported for the artifact routes in Task 12; leave the import in place.

- [ ] **Step 4: Implement `app/api/routes.py`**

```python
import logging
import shutil
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile, status

from app.api.deps import get_paths, get_pool, get_settings_from_request, get_store, require_api_key
from app.api.errors import ApiError
from app.api.schemas import JobCreated, JobDetail, JobLinks, build_job_detail, parse_options
from app.jobs.pool import QueueFull

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["documents"], dependencies=[Depends(require_api_key)])

RETRY_AFTER_SECONDS = "30"


def _job_base_url(job_id: str) -> str:
    return f"/v1/documents/{job_id}"


@router.post(
    "/documents",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobCreated,
    summary="Upload a document for conversion",
)
async def create_document(
    request: Request,
    file: UploadFile = File(..., description="The document to convert."),
    options: str | None = Form(default=None, description="Conversion options as a JSON object."),
) -> JobCreated:
    settings = get_settings_from_request(request)
    paths = get_paths(request)
    store = get_store(request)
    pool = get_pool(request)

    parsed = parse_options(options, settings)

    job_id = str(uuid4())
    paths.create(job_id)
    target = paths.input_file(job_id, file.filename or "upload")
    with target.open("wb") as destination:
        shutil.copyfileobj(file.file, destination)

    job = store.create(job_id, target.name, parsed.model_dump(mode="json"))

    try:
        pool.enqueue(job_id)
    except QueueFull as exc:
        store.delete(job_id)
        raise ApiError(
            503, "queue_full", "The conversion queue is at capacity. Retry shortly."
        ) from exc

    logger.info("Accepted job %s (%s)", job_id, target.name)
    return JobCreated(
        id=job.id,
        status=job.status,
        filename=job.filename,
        created_at=job.created_at,
        links=JobLinks(self=_job_base_url(job_id)),
    )


@router.get(
    "/documents/{job_id}",
    response_model=JobDetail,
    summary="Fetch job status and, once finished, every artifact link",
)
async def get_document(request: Request, job_id: str) -> JobDetail:
    job = get_store(request).get(job_id)
    if job is None:
        raise ApiError(404, "not_found", f"No job with id {job_id!r}.")
    return build_job_detail(job, _job_base_url(job_id))
```

- [ ] **Step 5: Add the `Retry-After` header and include the router**

In `app/api/errors.py`, change `_envelope` to carry headers, and make the `ApiError` handler set `Retry-After` for 503:

```python
def _envelope(
    status_code: int, code: str, message: str, headers: dict | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
        headers=headers,
    )
```

and inside `install_error_handlers`:

```python
    @app.exception_handler(ApiError)
    async def _handle_api_error(_: Request, exc: ApiError) -> JSONResponse:
        headers = {"Retry-After": "30"} if exc.status_code == 503 else None
        return _envelope(exc.status_code, exc.code, exc.message, headers)
```

In `app/main.py`, import and include the router just after the health router:

```python
from app.api.routes import router as documents_router
```

```python
    app.include_router(health_router)
    app.include_router(documents_router)
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_routes_documents.py -v`
Expected: PASS (11 tests)

- [ ] **Step 7: Commit**

```bash
git add app/api/routes.py app/api/schemas.py app/api/errors.py app/main.py tests/test_routes_documents.py
git commit -m "feat: add document upload and job status routes"
```

---

### Task 12: Artifact retrieval and delete routes

**Files:**
- Modify: `app/api/routes.py`
- Test: `tests/test_routes_artifacts.py`

**Interfaces:**
- Consumes: Task 11's router, `JobPaths` (Task 3), `JobStore` (Task 4).
- Produces, all on `router`:
  - `GET /v1/documents/{job_id}/markdown` → `text/markdown`
  - `GET /v1/documents/{job_id}/document.json` → `application/json`
  - `GET /v1/documents/{job_id}/tables/{index}?format=csv|html` → `text/csv` / `text/html`
  - `GET /v1/documents/{job_id}/images/{name}` → `image/png`
  - `DELETE /v1/documents/{job_id}` → 204
  - Internal helper `_require_succeeded(request, job_id) -> Job` — 404 unknown, 409 not yet finished or failed.

- [ ] **Step 1: Write the failing test**

`tests/test_routes_artifacts.py`:

```python
import json

import pytest

from app.conversion.service import ConversionFailed
from app.storage.paths import JobPaths

MANIFEST = {
    "page_count": 2,
    "warnings": [],
    "markdown": {"bytes": 11},
    "document": {"bytes": 17},
    "tables": [{"index": 0, "rows": 2, "cols": 2, "page": 1,
                "csv": "table-000.csv", "html": "table-000.html"}],
    "images": [{"name": "picture-000.png", "kind": "picture", "page": 2, "bytes": 3}],
}


@pytest.fixture
def finished_job(client, auth, settings, recording_service):
    """Upload a document and have the fake service write real artifact files."""
    paths = JobPaths(settings.data_dir)

    def run(job_id, input_path, options):
        paths.markdown_file(job_id).write_text("# Converted", encoding="utf-8")
        paths.document_file(job_id).write_text(json.dumps({"name": "doc"}), encoding="utf-8")
        paths.table_file(job_id, 0, "csv").write_text("a,b\n1,2\n", encoding="utf-8")
        paths.table_file(job_id, 0, "html").write_text("<table></table>", encoding="utf-8")
        paths.image_file(job_id, "picture-000.png").write_bytes(b"PNG")
        return MANIFEST

    recording_service.run = run

    job_id = client.post(
        "/v1/documents", headers=auth,
        files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")},
    ).json()["id"]
    assert client.app.state.pool.wait_idle(timeout=5)
    return job_id


def test_markdown_is_served(client, auth, finished_job):
    response = client.get(f"/v1/documents/{finished_job}/markdown", headers=auth)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.text == "# Converted"


def test_document_json_is_served(client, auth, finished_job):
    response = client.get(f"/v1/documents/{finished_job}/document.json", headers=auth)
    assert response.status_code == 200
    assert response.json() == {"name": "doc"}


def test_table_csv_is_default(client, auth, finished_job):
    response = client.get(f"/v1/documents/{finished_job}/tables/0", headers=auth)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.text == "a,b\n1,2\n"


def test_table_html_format(client, auth, finished_job):
    response = client.get(f"/v1/documents/{finished_job}/tables/0?format=html", headers=auth)
    assert response.status_code == 200
    assert response.text == "<table></table>"


def test_unknown_table_index_is_404(client, auth, finished_job):
    response = client.get(f"/v1/documents/{finished_job}/tables/9", headers=auth)
    assert response.status_code == 404


def test_bad_table_format_is_400(client, auth, finished_job):
    response = client.get(f"/v1/documents/{finished_job}/tables/0?format=pdf", headers=auth)
    assert response.status_code == 400


def test_image_is_served(client, auth, finished_job):
    response = client.get(
        f"/v1/documents/{finished_job}/images/picture-000.png", headers=auth
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == b"PNG"


def test_unknown_image_is_404(client, auth, finished_job):
    response = client.get(f"/v1/documents/{finished_job}/images/nope.png", headers=auth)
    assert response.status_code == 404


def test_image_traversal_is_rejected(client, auth, finished_job):
    response = client.get(
        f"/v1/documents/{finished_job}/images/..%2F..%2Fjobs.sqlite3", headers=auth
    )
    assert response.status_code in (400, 404)


def test_artifacts_require_a_key(client, finished_job):
    assert client.get(f"/v1/documents/{finished_job}/markdown").status_code == 401


def test_artifacts_of_a_failed_job_are_409(client, auth, recording_service):
    recording_service.raises = ConversionFailed("conversion_failed", "bad input")
    job_id = client.post(
        "/v1/documents", headers=auth,
        files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")},
    ).json()["id"]
    assert client.app.state.pool.wait_idle(timeout=5)

    response = client.get(f"/v1/documents/{job_id}/markdown", headers=auth)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


def test_artifacts_of_an_unknown_job_are_404(client, auth):
    assert client.get("/v1/documents/ghost/markdown", headers=auth).status_code == 404


def test_delete_removes_job_and_files(client, auth, settings, finished_job):
    paths = JobPaths(settings.data_dir)

    response = client.delete(f"/v1/documents/{finished_job}", headers=auth)

    assert response.status_code == 204
    assert not paths.job_dir(finished_job).exists()
    assert client.get(f"/v1/documents/{finished_job}", headers=auth).status_code == 404


def test_delete_unknown_job_is_404(client, auth):
    assert client.delete("/v1/documents/ghost", headers=auth).status_code == 404
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_routes_artifacts.py -v`
Expected: FAIL — artifact routes return 404 because they do not exist.

- [ ] **Step 3: Append the routes to `app/api/routes.py`**

Add to the imports at the top of the file:

```python
from typing import Literal

from fastapi.responses import FileResponse

from app.jobs.models import Job, JobStatus
```

Append:

```python
def _require_succeeded(request: Request, job_id: str) -> Job:
    """Return the job, or refuse if its artifacts cannot exist yet."""
    job = get_store(request).get(job_id)
    if job is None:
        raise ApiError(404, "not_found", f"No job with id {job_id!r}.")
    if job.status is JobStatus.FAILED:
        raise ApiError(409, "conflict", f"Job {job_id} failed; it has no artifacts.")
    if job.status is not JobStatus.SUCCEEDED:
        raise ApiError(
            409, "conflict", f"Job {job_id} is {job.status.value}; artifacts are not ready."
        )
    return job


def _serve(path, media_type: str, filename: str) -> FileResponse:
    if not path.exists():
        raise ApiError(404, "not_found", f"{filename} is not available for this job.")
    return FileResponse(path, media_type=media_type, filename=filename)


@router.get(
    "/documents/{job_id}/markdown",
    response_class=FileResponse,
    summary="Fetch the converted document as Markdown",
)
async def get_markdown(request: Request, job_id: str) -> FileResponse:
    _require_succeeded(request, job_id)
    return _serve(
        get_paths(request).markdown_file(job_id), "text/markdown; charset=utf-8", "document.md"
    )


@router.get(
    "/documents/{job_id}/document.json",
    response_class=FileResponse,
    summary="Fetch the full DoclingDocument JSON",
)
async def get_document_json(request: Request, job_id: str) -> FileResponse:
    _require_succeeded(request, job_id)
    return _serve(
        get_paths(request).document_file(job_id), "application/json", "document.json"
    )


@router.get(
    "/documents/{job_id}/tables/{index}",
    response_class=FileResponse,
    summary="Fetch one extracted table as CSV or HTML",
)
async def get_table(
    request: Request,
    job_id: str,
    index: int,
    format: Literal["csv", "html"] = "csv",
) -> FileResponse:
    job = _require_succeeded(request, job_id)
    tables = (job.manifest or {}).get("tables", [])
    if not any(entry["index"] == index for entry in tables):
        raise ApiError(404, "not_found", f"Job {job_id} has no table {index}.")

    media_type = "text/csv; charset=utf-8" if format == "csv" else "text/html; charset=utf-8"
    path = get_paths(request).table_file(job_id, index, format)
    return _serve(path, media_type, f"table-{index:03d}.{format}")


@router.get(
    "/documents/{job_id}/images/{name}",
    response_class=FileResponse,
    summary="Fetch one extracted image",
)
async def get_image(request: Request, job_id: str, name: str) -> FileResponse:
    job = _require_succeeded(request, job_id)
    images = (job.manifest or {}).get("images", [])
    if not any(entry["name"] == name for entry in images):
        raise ApiError(404, "not_found", f"Job {job_id} has no image {name!r}.")

    try:
        path = get_paths(request).image_file(job_id, name)
    except ValueError as exc:
        raise ApiError(400, "invalid_request", f"Illegal image name {name!r}.") from exc
    return _serve(path, "image/png", name)


@router.delete(
    "/documents/{job_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a job and every artifact it produced",
)
async def delete_document(request: Request, job_id: str) -> Response:
    if not get_store(request).delete(job_id):
        raise ApiError(404, "not_found", f"No job with id {job_id!r}.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
```

> The name lookup happens against the manifest before touching disk, so an unknown or hostile `name` never reaches the filesystem. `JobPaths.image_file` rejecting traversal is the second line of defence.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_routes_artifacts.py -v`
Expected: PASS (14 tests)

- [ ] **Step 5: Run the whole fast suite**

Run: `.venv/bin/pytest -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/api/routes.py tests/test_routes_artifacts.py
git commit -m "feat: serve markdown, JSON, tables, and images; add job deletion"
```

---

### Task 13: OpenAPI documentation

**Files:**
- Modify: `app/main.py`, `app/api/routes.py`
- Test: `tests/test_openapi.py`

**Interfaces:**
- Consumes: `api_key_header` (Task 2), the routes (Tasks 11–12), `ErrorEnvelope` (Task 11).
- Produces: a complete OpenAPI document — an `ApiKeyAuth` security scheme, declared error responses per route, a worked `options` example, tags, and an app-level description of the async flow.

- [ ] **Step 1: Write the failing test**

`tests/test_openapi.py`:

```python
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_schema_declares_the_api_key_scheme(client):
    schemes = client.get("/openapi.json").json()["components"]["securitySchemes"]
    assert schemes["APIKeyHeader"]["in"] == "header"
    assert schemes["APIKeyHeader"]["name"] == "X-API-Key"


def test_upload_route_declares_its_error_responses(client):
    responses = client.get("/openapi.json").json()["paths"]["/v1/documents"]["post"]["responses"]
    for code in ("202", "400", "401", "503"):
        assert code in responses


def test_upload_route_requires_the_scheme(client):
    operation = client.get("/openapi.json").json()["paths"]["/v1/documents"]["post"]
    assert {"APIKeyHeader": []} in operation["security"]


def test_health_route_is_not_secured(client):
    operation = client.get("/openapi.json").json()["paths"]["/health"]["get"]
    assert "security" not in operation


def test_artifact_routes_declare_409(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "409" in paths["/v1/documents/{job_id}/markdown"]["get"]["responses"]
    assert "409" in paths["/v1/documents/{job_id}/tables/{index}"]["get"]["responses"]


def test_options_field_carries_an_example(client):
    schema = client.get("/openapi.json").json()["components"]["schemas"]
    body = schema["Body_create_document_v1_documents_post"]["properties"]["options"]
    assert "example" in body or "examples" in body


def test_app_description_explains_the_flow(client):
    info = client.get("/openapi.json").json()["info"]
    assert "202" in info["description"]
    assert "poll" in info["description"].lower()


def test_docs_can_be_disabled(tmp_path, recording_service):
    settings = Settings(api_keys="k", data_dir=tmp_path, docs_enabled=False)
    app = create_app(settings=settings, service=recording_service)
    with TestClient(app) as client:
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/docs").status_code == 404


def test_docs_page_renders(client):
    assert client.get("/docs").status_code == 200
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_openapi.py -v`
Expected: FAIL — no declared error responses, no description, no example.

- [ ] **Step 3: Add the app description in `app/main.py`**

Add above `create_app`:

```python
API_DESCRIPTION = """
Convert documents with [docling](https://github.com/docling-project/docling).

Conversion is asynchronous:

1. `POST /v1/documents` with your file. The response is **202** with a job `id`.
2. Poll `GET /v1/documents/{id}` until `status` is `succeeded` or `failed`.
3. On success, `result` links every artifact: Markdown, the full DoclingDocument
   JSON, each table as CSV or HTML, and each extracted image.
4. `DELETE /v1/documents/{id}` removes the job and its files.

Every route except `/health` requires an `X-API-Key` header. Artifact routes answer
**409** while a job is still running, so "not ready" is distinguishable from "not found".
"""
```

and pass it to the `FastAPI(...)` constructor:

```python
        description=API_DESCRIPTION,
```

- [ ] **Step 4: Declare error responses and the options example in `app/api/routes.py`**

Add near the top, after the imports:

```python
from app.api.schemas import ErrorEnvelope

_UNAUTHORIZED = {401: {"model": ErrorEnvelope, "description": "Missing or invalid API key."}}
_NOT_FOUND = {404: {"model": ErrorEnvelope, "description": "No job with this id."}}
_INVALID = {400: {"model": ErrorEnvelope, "description": "The request was not valid."}}
_CONFLICT = {
    409: {"model": ErrorEnvelope, "description": "The job has not produced artifacts."}
}
_QUEUE_FULL = {
    503: {"model": ErrorEnvelope, "description": "The conversion queue is at capacity."}
}

OPTIONS_EXAMPLE = '{"ocr": true, "ocr_languages": ["en", "de"], "table_mode": "accurate", "image_scale": 2.0}'
```

Then extend each decorator with its `responses`, and give the `options` form field an example:

```python
@router.post(
    "/documents",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobCreated,
    summary="Upload a document for conversion",
    responses={**_INVALID, **_UNAUTHORIZED, **_QUEUE_FULL},
)
async def create_document(
    request: Request,
    file: UploadFile = File(..., description="The document to convert."),
    options: str | None = Form(
        default=None,
        description=(
            "Conversion options as a JSON object. Omit for server defaults. "
            "Unknown fields are rejected."
        ),
        example=OPTIONS_EXAMPLE,
    ),
) -> JobCreated:
```

Apply the matching `responses` to the rest:

- `get_document`: `responses={**_UNAUTHORIZED, **_NOT_FOUND}`
- `get_markdown`, `get_document_json`: `responses={**_UNAUTHORIZED, **_NOT_FOUND, **_CONFLICT}`
- `get_table`, `get_image`: `responses={**_INVALID, **_UNAUTHORIZED, **_NOT_FOUND, **_CONFLICT}`
- `delete_document`: `responses={**_UNAUTHORIZED, **_NOT_FOUND}`

> If `example=` on `Form(...)` is rejected by the installed FastAPI, use
> `openapi_examples={"default": {"value": OPTIONS_EXAMPLE}}` instead — the test
> accepts either `example` or `examples` in the generated schema.

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_openapi.py -v`
Expected: PASS (9 tests)

- [ ] **Step 6: Open the docs and confirm by eye**

```bash
API_KEYS=dev-key DATA_DIR=./data .venv/bin/uvicorn app.main:create_app --factory --port 8000
```

Visit `http://localhost:8000/docs`. Confirm: the *Authorize* button appears and accepts `dev-key`; `POST /v1/documents` shows the filled-in `options` example; the 409 and 503 responses are listed. Stop the server.

> Uploading here will load real models if docling is installed, or fail on import if it is not. Checking the page is the point of this step; end-to-end conversion is Task 14.

- [ ] **Step 7: Commit**

```bash
git add app/main.py app/api/routes.py tests/test_openapi.py
git commit -m "docs: complete the OpenAPI schema with auth, errors, and examples"
```

---

### Task 14: Container, compose, end-to-end test, README

**Files:**
- Create: `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `README.md`
- Create: `tests/fixtures/sample.pdf`, `tests/test_end_to_end.py`

**Interfaces:**
- Consumes: the whole application.
- Produces: a runnable image, and one `@pytest.mark.slow` test proving a real docling conversion end to end.

- [ ] **Step 1: Create the PDF fixture**

```bash
mkdir -p tests/fixtures
.venv/bin/python - <<'PY'
from pathlib import Path
from reportlab.pdfgen import canvas  # noqa
PY
```

If `reportlab` is not available, generate a minimal one-page PDF without it:

```bash
.venv/bin/python - <<'PY'
from pathlib import Path

objects = [
    b"<< /Type /Catalog /Pages 2 0 R >>",
    b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
    b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
    b"<< /Length 68 >>\nstream\nBT /F1 24 Tf 72 700 Td (Docling Extraction API test) Tj ET\nendstream",
    b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
]

out = bytearray(b"%PDF-1.4\n")
offsets = []
for number, body in enumerate(objects, start=1):
    offsets.append(len(out))
    out += b"%d 0 obj\n" % number + body + b"\nendobj\n"

xref_at = len(out)
out += b"xref\n0 %d\n" % (len(objects) + 1)
out += b"0000000000 65535 f \n"
for offset in offsets:
    out += b"%010d 00000 n \n" % offset
out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
    len(objects) + 1, xref_at
)

Path("tests/fixtures/sample.pdf").write_bytes(bytes(out))
print("wrote", Path("tests/fixtures/sample.pdf").stat().st_size, "bytes")
PY
```

Verify it opens: `open tests/fixtures/sample.pdf` (or any PDF viewer). It must render the line of text.

- [ ] **Step 2: Write the slow end-to-end test**

`tests/test_end_to_end.py`:

```python
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

pytest.importorskip("docling", reason="real docling not installed")

FIXTURE = Path(__file__).parent / "fixtures" / "sample.pdf"


@pytest.mark.slow
def test_real_conversion_produces_artifacts(tmp_path: Path):
    settings = Settings(
        api_keys="e2e-key",
        data_dir=tmp_path,
        worker_threads=1,
        job_timeout_seconds=600.0,
    )
    auth = {"X-API-Key": "e2e-key"}

    app = create_app(settings=settings)
    with TestClient(app) as client:
        created = client.post(
            "/v1/documents",
            headers=auth,
            files={"file": ("sample.pdf", FIXTURE.read_bytes(), "application/pdf")},
            data={"options": '{"ocr": false, "table_mode": "fast"}'},
        )
        assert created.status_code == 202
        job_id = created.json()["id"]

        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            payload = client.get(f"/v1/documents/{job_id}", headers=auth).json()
            if payload["status"] in ("succeeded", "failed"):
                break
            time.sleep(2)

        assert payload["status"] == "succeeded", payload.get("error")
        assert payload["result"]["page_count"] == 1

        markdown = client.get(f"/v1/documents/{job_id}/markdown", headers=auth)
        assert markdown.status_code == 200
        assert "Docling" in markdown.text

        document = client.get(f"/v1/documents/{job_id}/document.json", headers=auth)
        assert document.status_code == 200
        assert "texts" in document.json()

        assert client.delete(f"/v1/documents/{job_id}", headers=auth).status_code == 204
```

- [ ] **Step 3: Write the `Dockerfile`**

```dockerfile
# syntax=docker/dockerfile:1

FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# CPU-only torch wheels: several GB smaller than the CUDA default.
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision

COPY requirements-base.txt requirements.txt ./
RUN pip install -r requirements.txt

# Bake the model weights in so the container starts offline and cold-starts fast.
RUN docling-tools models download -o /opt/models


FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    DATA_DIR=/data \
    MODELS_DIR=/opt/models

# libgomp1 is required by torch; the rest are common image/PDF runtime deps.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 libglib2.0-0 libgl1 curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/models /opt/models

RUN useradd --create-home --uid 10001 docling \
    && mkdir -p /data \
    && chown -R docling:docling /data

WORKDIR /srv
COPY --chown=docling:docling app ./app
COPY --chown=docling:docling tests ./tests
COPY --chown=docling:docling pytest.ini ./

USER docling
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# One worker only: the queue and thread pool live in this process.
CMD ["uvicorn", "app.main:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
```

`.dockerignore`:

```
.venv/
.git/
__pycache__/
*.pyc
.pytest_cache/
data/
scratch/
docs/
```

- [ ] **Step 4: Write `docker-compose.yml`**

```yaml
services:
  api:
    build: .
    image: docling-extraction-api:latest
    ports:
      - "8000:8000"
    environment:
      API_KEYS: ${API_KEYS:-dev-key}
      DATA_DIR: /data
      MODELS_DIR: /opt/models
      WORKER_THREADS: ${WORKER_THREADS:-2}
      QUEUE_MAX: ${QUEUE_MAX:-32}
      JOB_TIMEOUT_SECONDS: ${JOB_TIMEOUT_SECONDS:-600}
      LOG_LEVEL: ${LOG_LEVEL:-INFO}
    volumes:
      - docling-data:/data
    restart: unless-stopped

volumes:
  docling-data:
```

- [ ] **Step 5: Build the image**

```bash
docker compose build
```

Expected: builds successfully. It downloads torch and the model weights, so expect several minutes and an image in the 4–6 GB range. Check with `docker images docling-extraction-api`.

- [ ] **Step 6: Run the container test suite, including the slow test**

```bash
docker compose run --rm -e API_KEYS=test api pytest -v -m "" tests/test_converters.py tests/test_end_to_end.py
```

Expected: PASS. This is the first run of `tests/test_converters.py` against real docling and the only run of the end-to-end conversion. `-m ""` clears the default `-m "not slow"` from `pytest.ini`.

> If the end-to-end test fails on a docling API call, that is the drift the spec warned about. Fix `app/conversion/converters.py` (and only that file) against the installed version, then re-run.

- [ ] **Step 7: Smoke-test the running service**

```bash
docker compose up -d
sleep 60  # models load on first conversion, not at boot; give the container time to start
curl -fsS http://localhost:8000/health

JOB=$(curl -fsS -X POST http://localhost:8000/v1/documents \
  -H "X-API-Key: dev-key" \
  -F "file=@tests/fixtures/sample.pdf" \
  -F 'options={"ocr": false, "table_mode": "fast"}' | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
echo "job: $JOB"

curl -fsS "http://localhost:8000/v1/documents/$JOB" -H "X-API-Key: dev-key"
# poll until status is succeeded, then:
curl -fsS "http://localhost:8000/v1/documents/$JOB/markdown" -H "X-API-Key: dev-key"
curl -fsS -X DELETE "http://localhost:8000/v1/documents/$JOB" -H "X-API-Key: dev-key" -o /dev/null -w "%{http_code}\n"

docker compose down
```

Expected: health returns `models_loaded: true`; the job reaches `succeeded`; markdown contains the fixture text; delete returns 204.

- [ ] **Step 8: Write `README.md`**

````markdown
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
| `MODELS_DIR` | `/opt/models` | Baked-in docling weights |
| `WORKER_THREADS` | `2` | Concurrent conversions |
| `QUEUE_MAX` | `32` | Queue capacity before 503 |
| `JOB_TIMEOUT_SECONDS` | `600` | Per-document docling timeout |
| `CONVERTER_CACHE_SIZE` | `4` | Cached converters per worker thread |
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
````

- [ ] **Step 9: Run the full fast suite one last time**

Run: `.venv/bin/pytest -v`
Expected: PASS, with `tests/test_converters.py` and `tests/test_end_to_end.py` skipped locally.

- [ ] **Step 10: Commit**

```bash
git add Dockerfile .dockerignore docker-compose.yml README.md tests/fixtures tests/test_end_to_end.py
git commit -m "feat: containerize the service and add an end-to-end conversion test"
```

---

## Self-review

**Spec coverage.** Every section of the spec maps to a task:

| Spec section | Task |
|---|---|
| `JobStore` + schema | 4 |
| `ConversionService` | 8 |
| `WorkerPool` | 9 |
| API layer | 11, 12 |
| Lifespan, crash recovery | 10 |
| Module layout | 1–12 (layout matches the spec's tree) |
| On-disk layout | 3 |
| Auth | 2 |
| Error envelope | 1, 11 |
| `POST /v1/documents` | 11 |
| `GET /v1/documents/{id}` | 11 |
| Artifact routes, 409 semantics | 12 |
| `DELETE` | 12 |
| `GET /health` | 1, 10 |
| Documentation (4 requirements) | 13 |
| Converter lifecycle | 7 |
| Artifact extraction | 6 |
| docling API drift | verified up front; guard notes in Tasks 6, 7, 14 |
| Failure handling table | 8 (conversion), 9 (worker), 4 + 10 (interrupted) |
| Testing | every task; slow test in 14 |
| Container, config table | 14 |
| Backlog | carried into README operational notes |

**Type consistency.** `JobStatus` values, `Job` field names, manifest keys (`markdown`/`document`/`tables`/`images`/`page_count`/`warnings`), table entry keys (`index`/`rows`/`cols`/`page`/`csv`/`html`), and image entry keys (`name`/`kind`/`page`/`bytes`) are identical across Tasks 6, 9, 11, and 12. `ConversionFailed.error_type` matches `JobStore.mark_failed(error_type, ...)`. `ConverterProvider.get(options)` matches the `FakeProvider.get` used in Task 8.

**Naming collision to watch:** `app.api.schemas.ImageRef` (a response model, Task 11) and `docling_core.types.doc.ImageRef` (used only in the Task 6 test) share a name. They never appear in the same module.

**Scope.** One plan, one deployable service. No task depends on a later one.
