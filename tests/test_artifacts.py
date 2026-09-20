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
    doc.add_table(
        data=TableData(num_rows=2, num_cols=2, table_cells=cells),
        prov=_prov(1),
        caption=doc.add_text(label="caption", text="Table 1 - counts"),
    )

    doc.add_picture(
        image=ImageRef.from_pil(Image.new("RGB", (8, 6), "red"), dpi=72),
        prov=_prov(2),
        caption=doc.add_text(label="caption", text="Fig 1 - a red square"),
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
        "caption": "Table 1 - counts",
        "self_ref": "#/tables/0",
        "bbox": {"l": 0.0, "t": 0.0, "r": 10.0, "b": 10.0, "coord_origin": "TOPLEFT"},
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
    assert entry["index"] == 0
    assert entry["width"] == 8
    assert entry["height"] == 6
    assert entry["caption"] == "Fig 1 - a red square"
    assert entry["self_ref"] == "#/pictures/0"
    assert entry["bbox"] == {
        "l": 0.0, "t": 0.0, "r": 10.0, "b": 10.0, "coord_origin": "TOPLEFT"
    }
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


def test_table_without_provenance_has_null_page(paths):
    doc = DoclingDocument(name="no-prov-table")
    cells = [
        TableCell(text="only", start_row_offset_idx=0, end_row_offset_idx=1,
                  start_col_offset_idx=0, end_col_offset_idx=1, column_header=True),
    ]
    doc.add_table(data=TableData(num_rows=1, num_cols=1, table_cells=cells))

    manifest = write_artifacts(doc, paths, "job-1")

    assert len(manifest["tables"]) == 1
    assert manifest["tables"][0] == {
        "index": 0, "rows": 1, "cols": 1, "page": None,
        "csv": "table-000.csv", "html": "table-000.html",
        "caption": None,
        "self_ref": "#/tables/0",
        # No provenance means no page *and* no bounding box.
        "bbox": None,
    }


def test_image_less_picture_is_skipped_without_disturbing_later_index(paths):
    doc = DoclingDocument(name="mixed-pictures")
    doc.add_picture(prov=_prov(1))  # no image attached: excluded from the manifest
    doc.add_picture(
        image=ImageRef.from_pil(Image.new("RGB", (8, 8), "blue"), dpi=72),
        prov=_prov(1),
    )

    manifest = write_artifacts(doc, paths, "job-1")

    assert len(manifest["images"]) == 1
    entry = manifest["images"][0]
    assert entry["name"] == "picture-001.png"
    assert entry["kind"] == "picture"
    assert entry["page"] == 1
    assert entry["bytes"] > 0
    # `index` is docling's picture index, not the position in this array:
    # picture 0 was skipped, so the sole entry is still index 1, and that
    # is what `self_ref` agrees with.
    assert entry["index"] == 1
    assert entry["self_ref"] == "#/pictures/1"
    assert paths.image_file("job-1", "picture-001.png").exists()


def test_a_filesystem_path_in_the_exception_never_reaches_the_manifest(
    document, paths, monkeypatch
):
    """A disk-full write during export must not leak the container's data
    directory layout to an HTTP caller.

    `manifest["warnings"]` is served verbatim as `JobResult.warnings` (see
    app/api/schemas.py); the same disclosure class was already fixed for
    job.error_message in app/jobs/pool.py (commit e1ccb8d) after an OSError's
    str() -- which embeds the full path being written to -- reached a
    caller. This reproduces that scenario for the table-export path.
    """
    from docling_core.types.doc import TableItem

    leaky_path = str(paths.table_file("job-1", 0, "csv"))

    def boom(self, doc=None, **kwargs):
        raise OSError(f"[Errno 28] No space left on device: {leaky_path!r}")

    monkeypatch.setattr(TableItem, "export_to_dataframe", boom)

    manifest = write_artifacts(document, paths, "job-1")

    assert len(manifest["warnings"]) == 1
    assert "OSError" in manifest["warnings"][0]
    assert leaky_path not in manifest["warnings"][0]
    assert str(paths.jobs_root) not in manifest["warnings"][0]


def test_a_failing_table_export_does_not_discard_the_conversion(document, paths, monkeypatch):
    """One unexportable table must not throw away a whole successful
    conversion. Before this, the exception escaped write_artifacts and the
    worker marked the job failed/internal_error -- discarding the markdown
    and document.json already on disk, plus every minute of OCR spent."""
    from docling_core.types.doc import TableItem

    def boom(self, doc=None, **kwargs):
        raise RuntimeError("dataframe export blew up")

    monkeypatch.setattr(TableItem, "export_to_dataframe", boom)

    manifest = write_artifacts(document, paths, "job-1")

    assert manifest["tables"] == []
    assert "Fixture Document" in paths.markdown_file("job-1").read_text(encoding="utf-8")
    assert json.loads(paths.document_file("job-1").read_text(encoding="utf-8"))["name"] == "fixture"
    assert len(manifest["warnings"]) == 1
    assert "Table 0" in manifest["warnings"][0]
    assert "RuntimeError" in manifest["warnings"][0]
    # The exception message itself is never served over HTTP -- only its type.
    assert "dataframe export blew up" not in manifest["warnings"][0]
    # The rest of the document survives intact.
    assert len(manifest["images"]) == 1


def test_a_failing_html_table_export_is_also_survivable(document, paths, monkeypatch):
    from docling_core.types.doc import TableItem

    def boom(self, doc=None, **kwargs):
        raise ValueError("html export blew up")

    monkeypatch.setattr(TableItem, "export_to_html", boom)

    manifest = write_artifacts(document, paths, "job-1")

    assert manifest["tables"] == []
    assert "ValueError" in manifest["warnings"][0]
    assert "html export blew up" not in manifest["warnings"][0]
    assert paths.markdown_file("job-1").exists()


def test_a_failing_picture_export_does_not_discard_the_conversion(document, paths, monkeypatch):
    from docling_core.types.doc import PictureItem

    def boom(self, doc=None, **kwargs):
        raise OSError("cannot write PNG")

    monkeypatch.setattr(PictureItem, "get_image", boom)

    manifest = write_artifacts(document, paths, "job-1")

    assert manifest["images"] == []
    assert len(manifest["tables"]) == 1
    assert paths.markdown_file("job-1").exists()
    assert paths.document_file("job-1").exists()
    assert len(manifest["warnings"]) == 1
    assert "Picture 0" in manifest["warnings"][0]
    assert "OSError" in manifest["warnings"][0]
    assert "cannot write PNG" not in manifest["warnings"][0]


def test_per_item_warnings_join_the_ones_passed_in(document, paths, monkeypatch):
    from docling_core.types.doc import TableItem

    monkeypatch.setattr(
        TableItem,
        "export_to_dataframe",
        lambda self, doc=None, **kwargs: (_ for _ in ()).throw(RuntimeError("nope")),
    )

    manifest = write_artifacts(document, paths, "job-1", warnings=["docling said so"])

    assert manifest["warnings"][0] == "docling said so"
    assert "Table 0" in manifest["warnings"][1]


def test_one_bad_table_does_not_stop_the_next_one(paths, monkeypatch):
    doc = DoclingDocument(name="two-tables")
    cells = [
        TableCell(text="only", start_row_offset_idx=0, end_row_offset_idx=1,
                  start_col_offset_idx=0, end_col_offset_idx=1, column_header=True),
    ]
    doc.add_table(data=TableData(num_rows=1, num_cols=1, table_cells=cells), prov=_prov(1))
    doc.add_table(data=TableData(num_rows=1, num_cols=1, table_cells=cells), prov=_prov(2))

    from docling_core.types.doc import TableItem

    real = TableItem.export_to_dataframe
    calls: list[int] = []

    def only_the_first_fails(self, doc=None, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("first table is broken")
        return real(self, doc=doc, **kwargs)

    monkeypatch.setattr(TableItem, "export_to_dataframe", only_the_first_fails)

    manifest = write_artifacts(doc, paths, "job-1")

    assert len(manifest["tables"]) == 1
    assert manifest["tables"][0]["index"] == 1
    assert len(manifest["warnings"]) == 1


def test_picture_kind_comes_from_the_docling_label(paths):
    """`PictureItem.label` is a DocItemLabel and is not always 'picture' --
    docling labels a chart as `chart`. The manifest used to hardcode
    'picture', losing that classification."""
    from docling_core.types.doc import DocItemLabel

    doc = DoclingDocument(name="chart-doc")
    doc.add_picture(
        image=ImageRef.from_pil(Image.new("RGB", (8, 8), "green"), dpi=72),
        prov=_prov(1),
    )
    doc.pictures[0].label = DocItemLabel.CHART

    manifest = write_artifacts(doc, paths, "job-1")

    assert manifest["images"][0]["kind"] == "chart"
    assert isinstance(manifest["images"][0]["kind"], str)


def test_picture_without_provenance_has_no_page_or_bbox(paths):
    doc = DoclingDocument(name="no-prov-picture")
    doc.add_picture(image=ImageRef.from_pil(Image.new("RGB", (4, 9), "green"), dpi=72))

    entry = write_artifacts(doc, paths, "job-1")["images"][0]

    assert entry["page"] is None
    assert entry["bbox"] is None
    # Dimensions come off the image itself, so they survive a missing prov.
    assert entry["width"] == 4
    assert entry["height"] == 9


def test_uncaptioned_items_report_null_not_empty_string(paths):
    """docling returns "" for a missing caption; the manifest stores None.

    Clients then have one absent-value check instead of two.
    """
    doc = DoclingDocument(name="uncaptioned")
    doc.add_picture(image=ImageRef.from_pil(Image.new("RGB", (2, 2), "blue"), dpi=72))
    cells = [
        TableCell(text="only", start_row_offset_idx=0, end_row_offset_idx=1,
                  start_col_offset_idx=0, end_col_offset_idx=1, column_header=True),
    ]
    doc.add_table(data=TableData(num_rows=1, num_cols=1, table_cells=cells))

    manifest = write_artifacts(doc, paths, "job-1")

    assert manifest["images"][0]["caption"] is None
    assert manifest["tables"][0]["caption"] is None


def test_bottom_left_coordinates_are_reported_as_such(paths):
    """PDF provenance is BOTTOMLEFT; the origin travels with the box.

    Without it a client cannot tell which way `t` and `b` run.
    """
    doc = DoclingDocument(name="bottom-left")
    doc.add_page(page_no=1, size=Size(width=600, height=800))
    doc.add_picture(
        image=ImageRef.from_pil(Image.new("RGB", (3, 3), "red"), dpi=72),
        prov=ProvenanceItem(
            page_no=1,
            bbox=BoundingBox(l=72, t=640.5, r=523.2, b=310.8,
                             coord_origin=CoordOrigin.BOTTOMLEFT),
            charspan=(0, 0),
        ),
    )

    assert write_artifacts(doc, paths, "job-1")["images"][0]["bbox"] == {
        "l": 72.0, "t": 640.5, "r": 523.2, "b": 310.8,
        "coord_origin": "BOTTOMLEFT",
    }


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
    assert "RuntimeError" in manifest["warnings"][0]
    assert "page export blew up" not in manifest["warnings"][0]
    assert len(manifest["images"]) == 1
    assert paths.document_file("job-1").exists()
