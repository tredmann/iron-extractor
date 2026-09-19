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
