"""The `/images` and `/tables` listing routes.

Enumeration used to live only on the job detail, so a client that wanted
images had to pull options, warnings, table refs and markdown refs to get
them. These two routes return just the one array, in exactly the shape the
job detail already uses.
"""

import pytest

from app.conversion.service import ConversionFailed

MANIFEST = {
    "page_count": 3,
    "warnings": [],
    "markdown": {"bytes": 10},
    "document": {"bytes": 20},
    "tables": [
        {
            "index": 0, "rows": 2, "cols": 3, "page": 1,
            "csv": "table-000.csv", "html": "table-000.html",
            "caption": "Table 1 - counts", "self_ref": "#/tables/0",
            "bbox": {"l": 1.0, "t": 2.0, "r": 3.0, "b": 4.0,
                     "coord_origin": "BOTTOMLEFT"},
        },
    ],
    "images": [
        {
            "index": 0, "name": "picture-000.png", "kind": "chart", "page": 2,
            "bytes": 512, "width": 1240, "height": 780,
            "caption": "Fig 1 - revenue", "self_ref": "#/pictures/0",
            "bbox": {"l": 72.0, "t": 640.5, "r": 523.2, "b": 310.8,
                     "coord_origin": "BOTTOMLEFT"},
        },
        {
            "index": 2, "name": "picture-002.png", "kind": "picture", "page": 3,
            "bytes": 256, "width": 64, "height": 64,
            "caption": None, "self_ref": "#/pictures/2", "bbox": None,
        },
    ],
}


def _upload(client, auth):
    return client.post(
        "/v1/documents",
        headers=auth,
        files={"file": ("report.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )


def _succeeded_job(client, auth, recording_service, manifest=MANIFEST):
    recording_service.manifest = manifest
    job_id = _upload(client, auth).json()["id"]
    assert recording_service.entered.wait(timeout=5)
    # Poll until the worker has committed the terminal state.
    for _ in range(200):
        if client.get(f"/v1/documents/{job_id}", headers=auth).json()["status"] == "succeeded":
            return job_id
    raise AssertionError("job never reached succeeded")


@pytest.mark.parametrize("kind", ["images", "tables"])
def test_listing_matches_the_job_detail_exactly(client, auth, recording_service, kind):
    """The one guard that keeps the shared builders honest.

    Both routes and `result` are fed by the same `build_*_refs` helper; if
    someone reimplements the manifest mapping in either place, the two
    payloads drift and this fails.
    """
    job_id = _succeeded_job(client, auth, recording_service)

    listing = client.get(f"/v1/documents/{job_id}/{kind}", headers=auth)
    assert listing.status_code == 200

    detail = client.get(f"/v1/documents/{job_id}", headers=auth).json()
    assert listing.json() == {kind: detail["result"][kind]}


def test_image_listing_carries_the_full_metadata(client, auth, recording_service):
    job_id = _succeeded_job(client, auth, recording_service)

    images = client.get(f"/v1/documents/{job_id}/images", headers=auth).json()["images"]

    assert images[0] == {
        "index": 0,
        "name": "picture-000.png",
        "kind": "chart",
        "page": 2,
        "width": 1240,
        "height": 780,
        "caption": "Fig 1 - revenue",
        "self_ref": "#/pictures/0",
        "bbox": {"l": 72.0, "t": 640.5, "r": 523.2, "b": 310.8,
                 "coord_origin": "BOTTOMLEFT"},
        "url": f"/v1/documents/{job_id}/images/picture-000.png",
        "bytes": 512,
    }
    # Absent metadata is null, not omitted.
    assert images[1]["caption"] is None
    assert images[1]["bbox"] is None
    # `index` follows docling, so a skipped picture leaves a gap.
    assert [entry["index"] for entry in images] == [0, 2]


def test_table_listing_carries_the_full_metadata(client, auth, recording_service):
    job_id = _succeeded_job(client, auth, recording_service)

    tables = client.get(f"/v1/documents/{job_id}/tables", headers=auth).json()["tables"]

    assert tables[0] == {
        "index": 0,
        "rows": 2,
        "cols": 3,
        "page": 1,
        "caption": "Table 1 - counts",
        "self_ref": "#/tables/0",
        "bbox": {"l": 1.0, "t": 2.0, "r": 3.0, "b": 4.0,
                 "coord_origin": "BOTTOMLEFT"},
        "csv_url": f"/v1/documents/{job_id}/tables/0?format=csv",
        "html_url": f"/v1/documents/{job_id}/tables/0?format=html",
    }


@pytest.mark.parametrize("kind", ["images", "tables"])
def test_listing_urls_actually_resolve(client, auth, recording_service, kind, settings):
    """A listing that hands out URLs nobody can fetch would be worthless.

    The artifact bytes are not on disk here (the service is a stand-in), so
    a 404 from the artifact route is the pass: it proves the URL routed to
    the right handler and got past the manifest membership check, rather
    than 405-ing or hitting the listing route again.
    """
    job_id = _succeeded_job(client, auth, recording_service)
    listing = client.get(f"/v1/documents/{job_id}/{kind}", headers=auth).json()[kind]

    url = listing[0]["url"] if kind == "images" else listing[0]["csv_url"]
    assert client.get(url, headers=auth).status_code == 404


@pytest.mark.parametrize("kind", ["images", "tables"])
def test_empty_document_lists_nothing_rather_than_404(client, auth, recording_service, kind):
    empty = {**MANIFEST, "tables": [], "images": []}
    job_id = _succeeded_job(client, auth, recording_service, manifest=empty)

    response = client.get(f"/v1/documents/{job_id}/{kind}", headers=auth)
    assert response.status_code == 200
    assert response.json() == {kind: []}


@pytest.mark.parametrize("kind", ["images", "tables"])
def test_listing_is_409_while_the_job_is_running(client, auth, recording_service, kind):
    recording_service.hold = True
    job_id = _upload(client, auth).json()["id"]
    assert recording_service.entered.wait(timeout=5)
    try:
        response = client.get(f"/v1/documents/{job_id}/{kind}", headers=auth)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "conflict"
    finally:
        recording_service.release()


@pytest.mark.parametrize("kind", ["images", "tables"])
def test_listing_is_409_for_a_failed_job(client, auth, recording_service, kind):
    recording_service.raises = ConversionFailed("timeout", "too slow")
    job_id = _upload(client, auth).json()["id"]
    assert recording_service.entered.wait(timeout=5)
    for _ in range(200):
        if client.get(f"/v1/documents/{job_id}", headers=auth).json()["status"] == "failed":
            break
    else:
        raise AssertionError("job never reached failed")

    assert client.get(f"/v1/documents/{job_id}/{kind}", headers=auth).status_code == 409


@pytest.mark.parametrize("kind", ["images", "tables"])
def test_listing_is_404_for_an_unknown_job(client, auth, kind):
    assert client.get(f"/v1/documents/does-not-exist/{kind}", headers=auth).status_code == 404


@pytest.mark.parametrize("kind", ["images", "tables"])
def test_listing_requires_a_key(client, kind):
    assert client.get(f"/v1/documents/whatever/{kind}").status_code == 401
