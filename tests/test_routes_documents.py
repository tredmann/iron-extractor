import json

from fastapi.testclient import TestClient

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


def test_queue_full_returns_503(client, auth, recording_service, settings, monkeypatch):
    from uuid import UUID

    import app.api.routes as routes
    from app.jobs.pool import QueueFull
    from app.storage.paths import JobPaths

    # A fixed, known id (rather than the real server-generated uuid4()) so
    # the test can assert on the exact row/directory that must NOT survive
    # a 503 -- QueueFull must leave no orphan behind.
    fixed_id = UUID("11111111-1111-1111-1111-111111111111")
    monkeypatch.setattr(routes, "uuid4", lambda: fixed_id)

    def always_full(_job_id: str) -> None:
        raise QueueFull(_job_id)

    monkeypatch.setattr(client.app.state.pool, "enqueue", always_full)

    response = _upload(client, auth)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "queue_full"
    assert response.headers["Retry-After"] == "30"

    assert client.app.state.store.get(str(fixed_id)) is None
    assert not JobPaths(settings.data_dir).job_dir(str(fixed_id)).exists()


def test_upload_cleans_up_the_job_directory_on_a_mid_write_failure(
    client, auth, settings, monkeypatch
):
    import app.api.routes as routes
    from app.storage.paths import JobPaths

    def boom(source, target):
        raise OSError("disk full")

    monkeypatch.setattr(routes, "_write_upload", boom)

    # The generic Exception handler renders a proper 500 response, but
    # Starlette's ServerErrorMiddleware always re-raises the original
    # exception afterwards too (by design, so servers can log it) --
    # TestClient's default raise_server_exceptions=True surfaces that as a
    # Python exception instead of a response. Use a lenient client against
    # the same already-running app to inspect the actual HTTP response.
    lenient_client = TestClient(client.app, raise_server_exceptions=False)
    response = _upload(lenient_client, auth)
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"

    assert list(JobPaths(settings.data_dir).jobs_root.iterdir()) == []


def test_mid_flight_job_is_pollable_with_a_null_result(client, auth, recording_service):
    recording_service.hold = True
    job_id = _upload(client, auth).json()["id"]
    assert recording_service.entered.wait(timeout=5)

    payload = client.get(f"/v1/documents/{job_id}", headers=auth).json()
    assert payload["status"] == "running"
    assert payload["result"] is None

    recording_service.release()
    assert client.app.state.pool.wait_idle(timeout=5)
