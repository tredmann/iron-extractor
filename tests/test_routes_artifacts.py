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


def test_image_traversal_with_a_slash_never_reaches_the_handler(client, auth, finished_job):
    """A slash-containing segment is rejected by Starlette's routing before
    `get_image` ever runs -- this is a routing-level 404, not evidence that
    either of this task's defences (manifest lookup, JobPaths.image_file)
    fired. See test_image_traversal_without_a_slash_is_rejected_by_the_manifest
    for a case that actually exercises the handler."""
    response = client.get(
        f"/v1/documents/{finished_job}/images/..%2F..%2Fjobs.sqlite3", headers=auth
    )
    assert response.status_code == 404


def test_image_traversal_without_a_slash_is_rejected_by_the_manifest(client, auth, finished_job):
    """An encoded literal '..' (no slash) does reach `get_image`. It fails the
    manifest membership check -- the first line of defence -- and comes back
    as the application's own 404 envelope naming the image, not a framework
    route-miss and not JobPaths.image_file's ValueError guard."""
    response = client.get(
        f"/v1/documents/{finished_job}/images/%2e%2e", headers=auth
    )
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert ".." in body["error"]["message"]


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


def test_artifacts_of_a_running_job_are_409(client, auth, recording_service):
    """A job that exists but hasn't finished must be distinguishable from one
    that never existed. Gate the fake service deterministically (no sleeps)
    so the job is observably `running` when we hit the artifact route."""
    recording_service.hold = True
    job_id = client.post(
        "/v1/documents", headers=auth,
        files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")},
    ).json()["id"]
    assert recording_service.entered.wait(timeout=5)

    response = client.get(f"/v1/documents/{job_id}/markdown", headers=auth)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"

    recording_service.release()
    assert client.app.state.pool.wait_idle(timeout=5)
