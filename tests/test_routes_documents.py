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
