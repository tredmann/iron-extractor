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
