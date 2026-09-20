# Per-Page Artifacts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `GET /v1/documents/{id}/pages` and `GET /v1/documents/{id}/pages/{page_no}`, returning each page's Markdown, flat text, images and tables inline in one response.

**Architecture:** Per-page Markdown and text are exported at conversion time by `docling`'s native `page_no=` parameter and written to `pages/page-001.md` / `page-001.txt`. The manifest gains a `pages[]` array holding filenames only. The routes read those files off the event loop and assemble responses, reusing the existing `build_image_refs` / `build_table_refs` helpers filtered by page so a page's entries are identical to the ones `/images` and `/tables` serve.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, `docling-core` (dev-installed, no torch), pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-09-20-per-page-artifacts-design.md`

## Global Constraints

These come from `CLAUDE.md` and the spec. Every task's requirements implicitly include them.

- **No module-level `import docling` anywhere in `app/`.** `app/conversion/artifacts.py` is duck-typed against `DoclingDocument` and imports nothing from docling at all — keep it that way.
- **Resolve the job from the store before constructing any filesystem path.**
- **Artifact routes resolve names from the manifest before touching disk.** 404 first; path validation is the second line of defence, not the first.
- **Every non-2xx response uses `{"error": {"code": ..., "message": ...}}`.**
- **The manifest is a cross-module contract.** `artifacts.write_artifacts` produces it and the HTTP layer reads it; changing a key breaks three modules.
- **`schemas.build_image_refs` / `build_table_refs` are the only place the HTTP layer reads the `images[]` / `tables[]` arrays.** Do not reimplement that mapping.
- **Blocking file I/O never runs on the event loop.** Use `run_in_threadpool`.
- **`ruff check app/ tests/` must stay clean.**
- Run the fast suite with `.venv/bin/pytest` — it deselects `slow` tests by design; expect a "deselected" line.

### Manifest shape this plan adds

Exact keys, copied from the spec. Task 2 writes them; Tasks 3 and 4 read them.

```json
"pages": [
  { "page": 1, "width": 612.0, "height": 792.0,
    "markdown": "page-001.md", "text": "page-001.txt" }
]
```

No `image_count` / `table_count`: `images[]` and `tables[]` already carry `page`.

---

### Task 1: JobPaths learns about the pages directory

**Files:**
- Modify: `app/storage/paths.py` (add two methods after `table_file`, extend `create`)
- Test: `tests/test_paths.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `JobPaths.pages_dir(job_id: str) -> Path` — `<job>/output/pages`
  - `JobPaths.page_file(job_id: str, page_no: int, fmt: str) -> Path` — `<job>/output/pages/page-001.md`
  - `JobPaths.create(job_id)` additionally creates `pages_dir`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_paths.py`:

```python
def test_page_file_naming(tmp_path: Path):
    paths = JobPaths(tmp_path)
    assert paths.page_file("job-1", 3, "md") == paths.pages_dir("job-1") / "page-003.md"
    assert paths.page_file("job-1", 3, "txt").name == "page-003.txt"


def test_page_numbers_beyond_three_digits_are_not_truncated(tmp_path: Path):
    """A 1000-page scan must not collide page 1024 with anything else."""
    paths = JobPaths(tmp_path)
    assert paths.page_file("job-1", 1024, "md").name == "page-1024.md"


def test_create_makes_the_pages_directory(tmp_path: Path):
    paths = JobPaths(tmp_path)
    paths.create("job-1")
    assert paths.pages_dir("job-1").is_dir()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_paths.py -q`
Expected: FAIL with `AttributeError: 'JobPaths' object has no attribute 'pages_dir'`

- [ ] **Step 3: Implement**

In `app/storage/paths.py`, add after `images_dir`:

```python
    def pages_dir(self, job_id: str) -> Path:
        return self.output_dir(job_id) / "pages"
```

Add after `table_file`:

```python
    def page_file(self, job_id: str, page_no: int, fmt: str) -> Path:
        """Resolve one page's exported artifact.

        `page_no` arrives as an int (it is an integer path parameter on the
        route, and a docling page key here), so unlike `image_file` there is
        no traversal surface and no containment check to make.
        """
        return self.pages_dir(job_id) / f"page-{page_no:03d}.{fmt}"
```

Extend `create` so the tuple reads:

```python
        for directory in (
            self.input_dir(job_id),
            self.output_dir(job_id),
            self.tables_dir(job_id),
            self.images_dir(job_id),
            self.pages_dir(job_id),
        ):
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_paths.py -q`
Expected: PASS

- [ ] **Step 5: Lint and commit**

```bash
.venv/bin/ruff check app/ tests/
git add app/storage/paths.py tests/test_paths.py
git commit -m "feat: JobPaths resolves per-page artifact files"
```

---

### Task 2: Conversion writes per-page Markdown and text

**Files:**
- Modify: `app/conversion/artifacts.py` (new `_write_pages`; wire into `write_artifacts`)
- Test: `tests/test_artifacts.py`

**Interfaces:**
- Consumes: `JobPaths.page_file(job_id, page_no, fmt)` from Task 1.
- Produces: `manifest["pages"]` — a list of `{"page": int, "width": float | None, "height": float | None, "markdown": str, "text": str}`, where `markdown` and `text` are **filenames**, and page-export failures are appended to `manifest["warnings"]`.

**Background the implementer needs:** `DoclingDocument.export_to_markdown(page_no=N)` and `.export_to_text(page_no=N)` both exist and take the page natively — no partitioning logic is needed. `document.pages` is a dict keyed by page number; `document.pages[n].size` has `.width` and `.height`. An out-of-range `page_no` returns `""` rather than raising. Content with **no provenance** appears in the whole-document export but in *no* page's export — that is docling's behaviour and is asserted below rather than worked around.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_artifacts.py`:

```python
def test_writes_one_markdown_and_one_text_file_per_page(document, paths):
    """Note: the `document` fixture's title and body copy have no provenance,
    so page 1 holds only the table and page 2 only the picture."""
    manifest = write_artifacts(document, paths, "job-1")

    assert [entry["page"] for entry in manifest["pages"]] == [1, 2]
    assert manifest["pages"][0] == {
        "page": 1, "width": 600.0, "height": 800.0,
        "markdown": "page-001.md", "text": "page-001.txt",
    }
    assert paths.page_file("job-1", 1, "md").exists()
    assert paths.page_file("job-1", 1, "txt").exists()


def test_page_exports_contain_only_that_page(paths):
    doc = DoclingDocument(name="two-pages")
    doc.add_page(page_no=1, size=Size(width=600, height=800))
    doc.add_page(page_no=2, size=Size(width=600, height=800))
    doc.add_text(label="text", text="ONLY ON ONE", prov=_prov(1))
    doc.add_text(label="text", text="ONLY ON TWO", prov=_prov(2))

    write_artifacts(doc, paths, "job-1")

    page1 = paths.page_file("job-1", 1, "md").read_text(encoding="utf-8")
    page2 = paths.page_file("job-1", 2, "md").read_text(encoding="utf-8")
    assert "ONLY ON ONE" in page1 and "ONLY ON TWO" not in page1
    assert "ONLY ON TWO" in page2 and "ONLY ON ONE" not in page2


def test_content_without_provenance_lands_on_no_page(paths):
    """docling cannot place it, so it is in the document but on no page.

    Asserted rather than fixed: pages genuinely do not sum to the document,
    and a client needing complete text must use /markdown. See the spec's
    "Known limitations".
    """
    doc = DoclingDocument(name="stray")
    doc.add_page(page_no=1, size=Size(width=600, height=800))
    doc.add_text(label="text", text="PLACED", prov=_prov(1))
    doc.add_text(label="text", text="UNPLACED")

    write_artifacts(doc, paths, "job-1")

    assert "UNPLACED" in paths.markdown_file("job-1").read_text(encoding="utf-8")
    assert "UNPLACED" not in paths.page_file("job-1", 1, "md").read_text(encoding="utf-8")


def test_text_export_is_flat_where_markdown_is_not(paths):
    """The .txt artifact drops markdown syntax the .md artifact keeps.

    Builds its own document rather than using the `document` fixture: that
    fixture's title and body copy carry no provenance, so they land on no
    page at all (see the no-provenance test above) and page 1 holds only
    the table.
    """
    doc = DoclingDocument(name="titled")
    doc.add_page(page_no=1, size=Size(width=600, height=800))
    doc.add_title(text="A Heading", prov=_prov(1))

    write_artifacts(doc, paths, "job-1")

    assert paths.page_file("job-1", 1, "md").read_text(encoding="utf-8") == "# A Heading"
    assert paths.page_file("job-1", 1, "txt").read_text(encoding="utf-8") == "A Heading"


def test_document_with_no_registered_pages_yields_no_page_entries(paths):
    doc = DoclingDocument(name="pageless")
    doc.add_text(label="text", text="floating")

    manifest = write_artifacts(doc, paths, "job-1")

    assert manifest["pages"] == []


def test_a_failing_page_export_does_not_discard_the_conversion(document, paths, monkeypatch):
    """One unexportable page must not throw away a whole conversion."""
    from docling_core.types.doc import DoclingDocument as _Doc

    def boom(self, **kwargs):
        raise RuntimeError("page export blew up")

    monkeypatch.setattr(_Doc, "export_to_markdown", boom)

    manifest = write_artifacts(document, paths, "job-1")

    assert manifest["pages"] == []
    assert len(manifest["warnings"]) == 2
    assert "Page 1" in manifest["warnings"][0]
    assert "page export blew up" in manifest["warnings"][0]
    # The rest of the document survives.
    assert len(manifest["images"]) == 1
    assert paths.document_file("job-1").exists()
```

Note: `monkeypatch.setattr` on `export_to_markdown` also breaks `_write_markdown`, so that test would fail for the wrong reason. Patch only the per-page call by asserting on the page loop instead — replace the last test above with this version, which patches `export_to_text` (used *only* by the page writer):

```python
def test_a_failing_page_export_does_not_discard_the_conversion(document, paths, monkeypatch):
    """One unexportable page must not throw away a whole conversion.

    `export_to_text` is patched rather than `export_to_markdown` because
    only the page writer calls it -- patching markdown would break the
    whole-document export too and the test would pass for the wrong reason.
    """
    from docling_core.types.doc import DoclingDocument as _Doc

    def boom(self, **kwargs):
        raise RuntimeError("page export blew up")

    monkeypatch.setattr(_Doc, "export_to_text", boom)

    manifest = write_artifacts(document, paths, "job-1")

    assert manifest["pages"] == []
    assert len(manifest["warnings"]) == 2
    assert "Page 1" in manifest["warnings"][0]
    assert "page export blew up" in manifest["warnings"][0]
    assert len(manifest["images"]) == 1
    assert paths.document_file("job-1").exists()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_artifacts.py -q`
Expected: FAIL with `KeyError: 'pages'`

- [ ] **Step 3: Implement**

In `app/conversion/artifacts.py`, add after `_write_pictures`:

```python
def _write_pages(document, paths: JobPaths, job_id: str) -> tuple[list[dict], list[str]]:
    """Export each page's markdown and flat text to its own file.

    docling takes `page_no` natively on both exporters, so there is no
    partitioning to do here. Defensive per page, like tables and pictures:
    one page that will not export must not discard a whole conversion.

    Note that pages do not necessarily sum to the document -- content
    docling could not place on a page appears in the whole-document export
    and in none of these.
    """
    entries: list[dict] = []
    warnings: list[str] = []
    for page_no in sorted(document.pages):
        try:
            markdown_target = paths.page_file(job_id, page_no, "md")
            text_target = paths.page_file(job_id, page_no, "txt")

            markdown_target.write_text(
                document.export_to_markdown(page_no=page_no), encoding="utf-8"
            )
            text_target.write_text(
                document.export_to_text(page_no=page_no), encoding="utf-8"
            )

            size = getattr(document.pages[page_no], "size", None)
            entry = {
                "page": page_no,
                "width": float(size.width) if size is not None else None,
                "height": float(size.height) if size is not None else None,
                "markdown": markdown_target.name,
                "text": text_target.name,
            }
        except Exception as exc:
            logger.exception("Job %s: page %s could not be exported", job_id, page_no)
            warnings.append(
                f"Page {page_no} could not be exported and was skipped: "
                f"{type(exc).__name__}: {exc}"
            )
            continue
        entries.append(entry)
    return entries, warnings
```

In `write_artifacts`, add the call after the pictures line:

```python
    images, picture_warnings = _write_pictures(document, paths, job_id)
    pages, page_warnings = _write_pages(document, paths, job_id)
```

and extend the returned dict — warnings gain `page_warnings`, and `"pages"` joins the manifest:

```python
        "warnings": [*(warnings or []), *table_warnings, *picture_warnings, *page_warnings],
        ...
        "images": images,
        "pages": pages,
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_artifacts.py -q`
Expected: PASS

- [ ] **Step 5: Run the whole fast suite**

Run: `.venv/bin/pytest -q`
Expected: PASS. Nothing else reads `manifest["pages"]` yet, so no other test should move.

- [ ] **Step 6: Lint and commit**

```bash
.venv/bin/ruff check app/ tests/
git add app/conversion/artifacts.py tests/test_artifacts.py
git commit -m "feat: export per-page markdown and text at conversion time"
```

---

### Task 3: Page response models and the pure builder

**Files:**
- Modify: `app/api/schemas.py` (models after `TableList`; builder after `build_table_refs`; `__all__`)
- Create: `tests/test_page_details.py`

**Interfaces:**
- Consumes: `manifest["pages"]` from Task 2; the existing `build_image_refs(job, base)` and `build_table_refs(job, base)`.
- Produces:
  - `PageDetail` — fields `page: int`, `width: float | None`, `height: float | None`, `markdown: str | None`, `text: str | None`, `images: list[ImageRef]`, `tables: list[TableRef]`
  - `PageList` — field `pages: list[PageDetail]`
  - `build_page_details(job: Job, base: str, contents: dict[int, tuple[str | None, str | None]], only: int | None = None) -> list[PageDetail]`

**Why `contents` is passed in:** the builder must stay a pure function so the schema layer performs no I/O. The route reads the files in a threadpool (Task 4) and hands the results here.

**Why `markdown`/`text` are nullable:** `None` means "listed in the manifest but unreadable" and stays distinguishable from `""`, which is what a page docling registered but extracted nothing from returns. A missing file must never sink a whole `/pages` response.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_page_details.py`:

```python
"""The pure builder behind the /pages routes.

No I/O and no HTTP: the route reads page files in a threadpool and hands the
contents in, so this stays a plain function over the manifest.
"""

from app.api.schemas import build_page_details
from app.jobs.models import Job, JobStatus

BASE = "/v1/documents/job-1"

MANIFEST = {
    "page_count": 2,
    "warnings": [],
    "markdown": {"bytes": 10},
    "document": {"bytes": 20},
    "pages": [
        {"page": 1, "width": 612.0, "height": 792.0,
         "markdown": "page-001.md", "text": "page-001.txt"},
        {"page": 2, "width": 612.0, "height": 792.0,
         "markdown": "page-002.md", "text": "page-002.txt"},
    ],
    "tables": [
        {"index": 0, "rows": 2, "cols": 2, "page": 1,
         "csv": "table-000.csv", "html": "table-000.html",
         "caption": None, "self_ref": "#/tables/0", "bbox": None},
    ],
    "images": [
        {"index": 0, "name": "picture-000.png", "kind": "picture", "page": 2,
         "bytes": 512, "width": 64, "height": 64,
         "caption": None, "self_ref": "#/pictures/0", "bbox": None},
        {"index": 1, "name": "picture-001.png", "kind": "chart", "page": None,
         "bytes": 256, "width": 32, "height": 32,
         "caption": None, "self_ref": "#/pictures/1", "bbox": None},
    ],
}

CONTENTS = {1: ("# One", "One"), 2: ("# Two", "Two")}


def _job(manifest=MANIFEST) -> Job:
    return Job(
        id="job-1",
        status=JobStatus.SUCCEEDED,
        filename="report.pdf",
        created_at="2026-09-20T00:00:00+00:00",
        options={},
        manifest=manifest,
    )


def test_builds_one_detail_per_manifest_page():
    details = build_page_details(_job(), BASE, CONTENTS)
    assert [detail.page for detail in details] == [1, 2]
    assert details[0].width == 612.0
    assert details[0].markdown == "# One"
    assert details[0].text == "One"


def test_images_and_tables_are_partitioned_by_page():
    details = build_page_details(_job(), BASE, CONTENTS)

    assert [table.index for table in details[0].tables] == [0]
    assert details[0].images == []
    assert [image.name for image in details[1].images] == ["picture-000.png"]
    assert details[1].tables == []


def test_items_with_no_page_appear_on_no_page():
    """picture-001 has page=None, so no page may claim it."""
    details = build_page_details(_job(), BASE, CONTENTS)
    names = [image.name for detail in details for image in detail.images]
    assert "picture-001.png" not in names


def test_entries_carry_the_same_urls_the_listing_routes_use():
    details = build_page_details(_job(), BASE, CONTENTS)
    assert details[1].images[0].url == f"{BASE}/images/picture-000.png"
    assert details[0].tables[0].csv_url == f"{BASE}/tables/0?format=csv"


def test_only_restricts_the_result_to_one_page():
    details = build_page_details(_job(), BASE, CONTENTS, only=2)
    assert [detail.page for detail in details] == [2]


def test_missing_content_is_null_not_empty():
    """None means unreadable; "" means a genuinely blank page."""
    details = build_page_details(_job(), BASE, {1: ("", ""), 2: (None, None)})
    assert details[0].markdown == "" and details[0].text == ""
    assert details[1].markdown is None and details[1].text is None


def test_a_manifest_without_pages_builds_nothing():
    details = build_page_details(_job({"images": [], "tables": []}), BASE, {})
    assert details == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_page_details.py -q`
Expected: FAIL with `ImportError: cannot import name 'build_page_details'`

- [ ] **Step 3: Add the models**

In `app/api/schemas.py`, after `class TableList`:

```python
class PageDetail(BaseModel):
    """One page of a document, with its content inline.

    `markdown` and `text` are null only when the page is listed in the
    manifest but its file could not be read -- which stays distinguishable
    from "", what a page docling registered but extracted nothing from
    returns.
    """

    page: int
    width: float | None = None
    height: float | None = None
    markdown: str | None = Field(
        default=None, description="This page's Markdown, or null if unreadable."
    )
    text: str | None = Field(
        default=None, description="This page's flat text, or null if unreadable."
    )
    images: list[ImageRef] = []
    tables: list[TableRef] = []


class PageList(BaseModel):
    """Every page of a document, content inline.

    Response size grows with the document and is deliberately unbounded;
    `/pages/{page_no}` is the bounded alternative.
    """

    pages: list[PageDetail] = []
```

Add `"PageDetail"`, `"PageList"` and `"build_page_details"` to `__all__`, keeping it alphabetical.

- [ ] **Step 4: Add the builder**

After `build_table_refs`:

```python
def build_page_details(
    job: Job,
    base: str,
    contents: dict[int, tuple[str | None, str | None]],
    only: int | None = None,
) -> list[PageDetail]:
    """Assemble per-page responses from the manifest and pre-read content.

    Pure: `contents` maps a page number to its `(markdown, text)` pair and is
    read by the route in a threadpool, so no file access happens here.

    Images and tables come from the shared builders and are filtered by page,
    which is what makes a page's entries byte-identical to the ones `/images`
    and `/tables` serve. Items with no page -- docling could not place them --
    are claimed by no page.
    """
    images = build_image_refs(job, base)
    tables = build_table_refs(job, base)

    details: list[PageDetail] = []
    for entry in (job.manifest or {}).get("pages", []):
        page_no = entry["page"]
        if only is not None and page_no != only:
            continue
        markdown, text = contents.get(page_no, (None, None))
        details.append(
            PageDetail(
                page=page_no,
                width=entry.get("width"),
                height=entry.get("height"),
                markdown=markdown,
                text=text,
                images=[image for image in images if image.page == page_no],
                tables=[table for table in tables if table.page == page_no],
            )
        )
    return details
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_page_details.py -q`
Expected: PASS

- [ ] **Step 6: Run the whole fast suite, lint, commit**

```bash
.venv/bin/pytest -q
.venv/bin/ruff check app/ tests/
git add app/api/schemas.py tests/test_page_details.py
git commit -m "feat: page response models and builder"
```

---

### Task 4: The two page routes

**Files:**
- Modify: `app/api/routes.py` (imports; two helpers; two routes placed before `delete_document`)
- Create: `tests/test_routes_pages.py`
- Modify: `tests/test_openapi.py`

**Interfaces:**
- Consumes: `JobPaths.page_file` (Task 1), `manifest["pages"]` (Task 2), `PageDetail` / `PageList` / `build_page_details` (Task 3).
- Produces: `GET /v1/documents/{job_id}/pages` → `PageList`; `GET /v1/documents/{job_id}/pages/{page_no}` → `PageDetail`.

**Background the implementer needs:** `_require_succeeded(request, job_id)` already returns the job or raises 404/409 — use it, do not reimplement. `page_no` is an `int` path parameter, so a non-integer value raises `RequestValidationError`, which `app/main.py`'s handler rewrites to **400 `invalid_request`** (the auto-422 is stripped from the schema). Declare `**_INVALID` on the detail route accordingly, matching `tables/{index}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_routes_pages.py`:

```python
"""The /pages routes.

Content is returned inline, so these are the only handlers that read
artifact files themselves rather than handing Starlette a FileResponse --
which is why the reads are offloaded to a threadpool.
"""

import pytest

from app.conversion.service import ConversionFailed
from app.storage.paths import JobPaths

MANIFEST = {
    "page_count": 2,
    "warnings": [],
    "markdown": {"bytes": 10},
    "document": {"bytes": 20},
    "pages": [
        {"page": 1, "width": 612.0, "height": 792.0,
         "markdown": "page-001.md", "text": "page-001.txt"},
        {"page": 2, "width": 612.0, "height": 792.0,
         "markdown": "page-002.md", "text": "page-002.txt"},
    ],
    "tables": [
        {"index": 0, "rows": 2, "cols": 2, "page": 1,
         "csv": "table-000.csv", "html": "table-000.html",
         "caption": "T1", "self_ref": "#/tables/0", "bbox": None},
    ],
    "images": [
        {"index": 0, "name": "picture-000.png", "kind": "picture", "page": 2,
         "bytes": 512, "width": 64, "height": 64,
         "caption": None, "self_ref": "#/pictures/0", "bbox": None},
    ],
}


def _upload(client, auth):
    return client.post(
        "/v1/documents",
        headers=auth,
        files={"file": ("report.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )


def _succeeded_job(client, auth, recording_service, settings, *, write_files=True):
    """Run a job to success and lay down the page files it claims to have."""
    recording_service.manifest = MANIFEST
    job_id = _upload(client, auth).json()["id"]
    assert recording_service.entered.wait(timeout=5)
    for _ in range(200):
        if client.get(f"/v1/documents/{job_id}", headers=auth).json()["status"] == "succeeded":
            break
    else:
        raise AssertionError("job never reached succeeded")

    if write_files:
        paths = JobPaths(settings.data_dir)
        for page_no in (1, 2):
            paths.page_file(job_id, page_no, "md").write_text(
                f"# Page {page_no}", encoding="utf-8"
            )
            paths.page_file(job_id, page_no, "txt").write_text(
                f"Page {page_no}", encoding="utf-8"
            )
    return job_id


def test_pages_returns_every_page_with_content_inline(client, auth, recording_service, settings):
    job_id = _succeeded_job(client, auth, recording_service, settings)

    response = client.get(f"/v1/documents/{job_id}/pages", headers=auth)

    assert response.status_code == 200
    pages = response.json()["pages"]
    assert [page["page"] for page in pages] == [1, 2]
    assert pages[0]["markdown"] == "# Page 1"
    assert pages[0]["text"] == "Page 1"
    assert pages[0]["width"] == 612.0


def test_single_page_route_returns_one_page(client, auth, recording_service, settings):
    job_id = _succeeded_job(client, auth, recording_service, settings)

    response = client.get(f"/v1/documents/{job_id}/pages/2", headers=auth)

    assert response.status_code == 200
    assert response.json()["page"] == 2
    assert response.json()["markdown"] == "# Page 2"


def test_single_page_matches_its_entry_in_the_full_listing(client, auth, recording_service, settings):
    job_id = _succeeded_job(client, auth, recording_service, settings)

    one = client.get(f"/v1/documents/{job_id}/pages/1", headers=auth).json()
    listing = client.get(f"/v1/documents/{job_id}/pages", headers=auth).json()["pages"]

    assert one == listing[0]


def test_images_and_tables_are_partitioned_by_page(client, auth, recording_service, settings):
    job_id = _succeeded_job(client, auth, recording_service, settings)

    pages = client.get(f"/v1/documents/{job_id}/pages", headers=auth).json()["pages"]

    assert [table["index"] for table in pages[0]["tables"]] == [0]
    assert pages[0]["images"] == []
    assert [image["name"] for image in pages[1]["images"]] == ["picture-000.png"]
    assert pages[1]["tables"] == []


def test_page_entries_are_identical_to_the_listing_routes(client, auth, recording_service, settings):
    """The guard on the shared builders.

    A page's images must be exactly the /images entries carrying that page.
    """
    job_id = _succeeded_job(client, auth, recording_service, settings)

    pages = client.get(f"/v1/documents/{job_id}/pages", headers=auth).json()["pages"]
    all_images = client.get(f"/v1/documents/{job_id}/images", headers=auth).json()["images"]
    all_tables = client.get(f"/v1/documents/{job_id}/tables", headers=auth).json()["tables"]

    for page in pages:
        assert page["images"] == [i for i in all_images if i["page"] == page["page"]]
        assert page["tables"] == [t for t in all_tables if t["page"] == page["page"]]


def test_unreadable_page_file_is_null_rather_than_an_error(client, auth, recording_service, settings):
    """One missing file must not sink the whole response."""
    job_id = _succeeded_job(client, auth, recording_service, settings, write_files=False)

    response = client.get(f"/v1/documents/{job_id}/pages", headers=auth)

    assert response.status_code == 200
    pages = response.json()["pages"]
    assert pages[0]["markdown"] is None
    assert pages[0]["text"] is None
    # The page is still listed, with its metadata intact.
    assert pages[0]["page"] == 1
    assert pages[0]["width"] == 612.0


def test_unknown_page_number_is_404(client, auth, recording_service, settings):
    job_id = _succeeded_job(client, auth, recording_service, settings)

    response = client.get(f"/v1/documents/{job_id}/pages/99", headers=auth)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_non_integer_page_number_is_400(client, auth, recording_service, settings):
    job_id = _succeeded_job(client, auth, recording_service, settings)

    response = client.get(f"/v1/documents/{job_id}/pages/abc", headers=auth)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_document_with_no_pages_lists_nothing(client, auth, recording_service, settings):
    recording_service.manifest = {**MANIFEST, "pages": []}
    job_id = _upload(client, auth).json()["id"]
    assert recording_service.entered.wait(timeout=5)
    for _ in range(200):
        if client.get(f"/v1/documents/{job_id}", headers=auth).json()["status"] == "succeeded":
            break

    response = client.get(f"/v1/documents/{job_id}/pages", headers=auth)
    assert response.status_code == 200
    assert response.json() == {"pages": []}


@pytest.mark.parametrize("path", ["pages", "pages/1"])
def test_is_409_while_the_job_is_running(client, auth, recording_service, path):
    recording_service.hold = True
    job_id = _upload(client, auth).json()["id"]
    assert recording_service.entered.wait(timeout=5)
    try:
        response = client.get(f"/v1/documents/{job_id}/{path}", headers=auth)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "conflict"
    finally:
        recording_service.release()


@pytest.mark.parametrize("path", ["pages", "pages/1"])
def test_is_409_for_a_failed_job(client, auth, recording_service, path):
    recording_service.raises = ConversionFailed("timeout", "too slow")
    job_id = _upload(client, auth).json()["id"]
    assert recording_service.entered.wait(timeout=5)
    for _ in range(200):
        if client.get(f"/v1/documents/{job_id}", headers=auth).json()["status"] == "failed":
            break

    assert client.get(f"/v1/documents/{job_id}/{path}", headers=auth).status_code == 409


@pytest.mark.parametrize("path", ["pages", "pages/1"])
def test_is_404_for_an_unknown_job(client, auth, path):
    assert client.get(f"/v1/documents/nope/{path}", headers=auth).status_code == 404


@pytest.mark.parametrize("path", ["pages", "pages/1"])
def test_requires_a_key(client, path):
    assert client.get(f"/v1/documents/whatever/{path}").status_code == 401
```

Append to `tests/test_openapi.py`:

```python
def test_page_routes_declare_their_error_responses(client):
    paths = client.get("/openapi.json").json()["paths"]
    for code in ("200", "401", "404", "409"):
        assert code in paths["/v1/documents/{job_id}/pages"]["get"]["responses"]
    detail = paths["/v1/documents/{job_id}/pages/{page_no}"]["get"]["responses"]
    for code in ("200", "400", "401", "404", "409"):
        assert code in detail
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_routes_pages.py tests/test_openapi.py -q`
Expected: FAIL — the page routes 404 because they do not exist yet.

- [ ] **Step 3: Extend the schema imports**

In `app/api/routes.py`, add to the `from app.api.schemas import (...)` block, keeping it alphabetical: `PageDetail`, `PageList`, `build_page_details`.

- [ ] **Step 4: Add the file-reading helpers**

In `app/api/routes.py`, add after `_serve`:

```python
def _read_page_text(path: Path) -> str | None:
    """A page artifact's contents, or None when it cannot be read.

    None means "the manifest lists this page but its file is gone", which
    stays distinguishable from "" -- a page docling registered but
    extracted nothing from. Returning None rather than raising is what
    stops one unreadable file from sinking a whole /pages response.
    """
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _read_page_contents(paths, job: Job, job_id: str, only: int | None = None) -> dict:
    """Read every requested page's markdown and text.

    Synchronous and called via run_in_threadpool: this is the one place the
    API reads artifact bytes itself instead of handing Starlette a
    FileResponse, and on a long document it is many reads. Doing them on the
    event loop would stall every other request in this single-process server.
    """
    contents = {}
    for entry in (job.manifest or {}).get("pages", []):
        page_no = entry["page"]
        if only is not None and page_no != only:
            continue
        contents[page_no] = (
            _read_page_text(paths.page_file(job_id, page_no, "md")),
            _read_page_text(paths.page_file(job_id, page_no, "txt")),
        )
    return contents
```

- [ ] **Step 5: Add the routes**

In `app/api/routes.py`, add immediately before the `@router.delete` block:

```python
@router.get(
    "/documents/{job_id}/pages",
    response_model=PageList,
    summary="Fetch every page of a document, content inline",
    responses={**_UNAUTHORIZED, **_NOT_FOUND, **_CONFLICT},
)
async def list_pages(request: Request, job_id: str) -> PageList:
    """Return every page with its Markdown and flat text inline.

    Response size grows with the document and is deliberately unbounded --
    one request gets the whole document page by page. Use
    `/pages/{page_no}` where a bounded response matters.
    """
    job = _require_succeeded(request, job_id)
    contents = await run_in_threadpool(
        _read_page_contents, get_paths(request), job, job_id
    )
    return PageList(pages=build_page_details(job, _job_base_url(job_id), contents))


@router.get(
    "/documents/{job_id}/pages/{page_no}",
    response_model=PageDetail,
    summary="Fetch one page, content inline",
    responses={**_INVALID, **_UNAUTHORIZED, **_NOT_FOUND, **_CONFLICT},
)
async def get_page(request: Request, job_id: str, page_no: int) -> PageDetail:
    """Return one page's Markdown, flat text, images and tables.

    The page is resolved from the manifest first: docling's exporter returns
    an empty string for an out-of-range page rather than raising, so without
    this check page 99 of a two-page document would look blank instead of
    missing.
    """
    job = _require_succeeded(request, job_id)
    pages = (job.manifest or {}).get("pages", [])
    if not any(entry["page"] == page_no for entry in pages):
        raise ApiError(404, "not_found", f"Job {job_id} has no page {page_no}.")

    contents = await run_in_threadpool(
        _read_page_contents, get_paths(request), job, job_id, page_no
    )
    details = build_page_details(job, _job_base_url(job_id), contents, only=page_no)
    return details[0]
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_routes_pages.py tests/test_openapi.py -q`
Expected: PASS

- [ ] **Step 7: Run the whole fast suite, lint, commit**

```bash
.venv/bin/pytest -q
.venv/bin/ruff check app/ tests/
git add app/api/routes.py tests/test_routes_pages.py tests/test_openapi.py
git commit -m "feat: add /pages and /pages/{page_no} routes"
```

---

### Task 5: Documentation and verification against real docling

**Files:**
- Modify: `README.md` (Flow section; a new "Pages" subsection; the two limitations)
- Modify: `CLAUDE.md` (manifest key list; a blocking-I/O invariant)

**Interfaces:**
- Consumes: everything from Tasks 1–4.
- Produces: no code.

- [ ] **Step 1: Document the routes in README.md**

After the "Listing images and tables" subsection, add:

````markdown
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
````

Also add to the Flow section's step 3, alongside the existing listing calls:

```bash
curl http://localhost:8000/v1/documents/<id>/pages   -H "X-API-Key: dev-key"
curl http://localhost:8000/v1/documents/<id>/pages/1 -H "X-API-Key: dev-key"
```

- [ ] **Step 2: Update the manifest invariant in CLAUDE.md**

In the "**The manifest is a cross-module contract.**" paragraph, extend the key
list with `pages[].page,width,height,markdown,text`, and add to the paragraph
about the shared builders that `build_page_details` is the only place the HTTP
layer reads `pages[]`.

- [ ] **Step 3: Add the blocking-I/O invariant to CLAUDE.md**

Add a new invariant before "**Exactly one uvicorn worker.**":

```markdown
**The `/pages` routes are the only handlers that read artifact bytes
themselves.** Every other artifact route hands Starlette a `FileResponse`,
which it serves off the loop. `/pages` inlines content, so it reads the files
directly -- and on a long document that is many reads. They go through
`run_in_threadpool` for that reason; doing them inline would stall every other
request in this single-process server. `_read_page_text` returns `None` for an
unreadable file rather than raising, so one missing file cannot sink a whole
`/pages` response.
```

- [ ] **Step 4: Verify against real docling in the container**

```bash
docker compose build api test
docker compose run --rm -e API_KEYS=test test pytest -m "" -q
```

Expected: PASS. This is the only place the code runs against real docling, and
it confirms `export_to_markdown(page_no=...)` behaves as the fast suite assumes.

- [ ] **Step 5: Verify the routes on a live document**

```bash
docker compose up -d --force-recreate api
KEY=$(grep -E '^API_KEYS=' .env | cut -d= -f2- | cut -d, -f1)
ID=$(curl -s -X POST localhost:8000/v1/documents -H "X-API-Key: $KEY" \
  -F file=@tests/fixtures/sample.pdf | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
# poll until succeeded, then:
curl -s localhost:8000/v1/documents/$ID/pages   -H "X-API-Key: $KEY" | python3 -m json.tool
curl -s localhost:8000/v1/documents/$ID/pages/1 -H "X-API-Key: $KEY" | python3 -m json.tool
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/v1/documents/$ID/pages/99 -H "X-API-Key: $KEY"
```

Expected: the listing carries real per-page Markdown and text; `/pages/1` equals
the first entry of the listing; `/pages/99` is `404`. Delete the job afterwards.

- [ ] **Step 6: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: document the per-page routes and their limitations"
```

---

## Self-review notes

Checked against the spec:

- Every spec section maps to a task: storage → Task 1, conversion + manifest →
  Task 2, models and builder → Task 3, routes and threadpool → Task 4, docs and
  both known limitations → Task 5.
- The no-provenance limitation is asserted by a test (Task 2), not just
  documented, so it cannot regress silently.
- The `null` vs `""` distinction from the spec's error-handling section is
  pinned in Tasks 3 and 4.
- Names are consistent across tasks: `pages_dir`, `page_file`,
  `_write_pages`, `PageDetail`, `PageList`, `build_page_details`,
  `_read_page_text`, `_read_page_contents`.
- One trap is called out inline in Task 2: patching `export_to_markdown` to test
  the failure path would also break the whole-document export and pass for the
  wrong reason, so `export_to_text` is patched instead.
- A second trap was caught during review and corrected: the shared `document`
  fixture adds its title and body copy **without provenance**, so page 1 holds
  only the table (`'| h1   |\n|------|'`) and page 2 only the picture. The
  flat-text test therefore builds its own document; asserting "Fixture Document"
  appears on page 1 would have failed. Verified against `docling-core`.
- `Job` is a frozen dataclass (`app/jobs/models.py`), not a Pydantic model;
  Task 3's test constructs it with keyword arguments including the required
  `options`.
