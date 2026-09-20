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
    else:
        raise AssertionError("job never reached failed")

    assert client.get(f"/v1/documents/{job_id}/{path}", headers=auth).status_code == 409


@pytest.mark.parametrize("path", ["pages", "pages/1"])
def test_is_404_for_an_unknown_job(client, auth, path):
    assert client.get(f"/v1/documents/nope/{path}", headers=auth).status_code == 404


@pytest.mark.parametrize("path", ["pages", "pages/1"])
def test_requires_a_key(client, path):
    assert client.get(f"/v1/documents/whatever/{path}").status_code == 401
