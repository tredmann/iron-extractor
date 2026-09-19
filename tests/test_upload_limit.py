"""The upload size limit, and why it cannot be a dependency.

FastAPI parses the request body before it resolves dependencies
(`await request.form()` precedes `solve_dependencies` in
`fastapi/routing.py`), so `require_api_key` runs only after the whole
multipart upload has been spooled -- and Starlette spools file parts to a
`SpooledTemporaryFile` that rolls over to disk past 1MB. Without an ASGI
middleware ahead of routing, an unauthenticated client can fill the
container's filesystem against a route that advertises 401.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _small_limit_client(tmp_path: Path, recording_service, limit: int = 1024):
    settings = Settings(
        api_keys="test-key",
        data_dir=tmp_path,
        worker_threads=1,
        queue_max=4,
        max_upload_bytes=limit,
    )
    return TestClient(create_app(settings=settings, service=recording_service))


def test_oversized_upload_is_413_without_a_key(tmp_path: Path, recording_service):
    """No API key at all: the refusal must happen before anything is read."""
    with _small_limit_client(tmp_path, recording_service) as client:
        response = client.post(
            "/v1/documents",
            files={"file": ("big.pdf", b"x" * 4096, "application/pdf")},
        )

    assert response.status_code == 413
    assert response.json() == {
        "error": {
            "code": "payload_too_large",
            "message": "The request body is larger than the 1024 byte limit.",
        }
    }


def test_oversized_upload_writes_nothing_to_disk(tmp_path: Path, recording_service):
    with _small_limit_client(tmp_path, recording_service) as client:
        client.post(
            "/v1/documents",
            headers={"X-API-Key": "test-key"},
            files={"file": ("big.pdf", b"x" * 4096, "application/pdf")},
        )

    jobs_root = tmp_path / "jobs"
    assert not jobs_root.exists() or list(jobs_root.iterdir()) == []


def test_oversized_upload_is_413_even_with_a_valid_key(tmp_path: Path, recording_service):
    with _small_limit_client(tmp_path, recording_service) as client:
        response = client.post(
            "/v1/documents",
            headers={"X-API-Key": "test-key"},
            files={"file": ("big.pdf", b"x" * 4096, "application/pdf")},
        )
    assert response.status_code == 413


def test_a_normal_upload_still_works(tmp_path: Path, recording_service):
    with _small_limit_client(tmp_path, recording_service) as client:
        response = client.post(
            "/v1/documents",
            headers={"X-API-Key": "test-key"},
            files={"file": ("small.pdf", b"%PDF-1.4", "application/pdf")},
        )
    assert response.status_code == 202


def test_the_generous_default_does_not_get_in_the_way(client, auth):
    response = client.post(
        "/v1/documents",
        headers=auth,
        files={"file": ("a.pdf", b"%PDF-1.4" + b"y" * 200_000, "application/pdf")},
    )
    assert response.status_code == 202


def test_default_limit_is_200mb():
    assert Settings(api_keys="k", data_dir=Path("/tmp/x")).max_upload_bytes == 200 * 1024 * 1024


def test_a_request_without_content_length_passes_through(tmp_path: Path, recording_service):
    """Chunked bodies declare no length; refusing them all would break
    legitimate clients, so they are passed through."""
    with _small_limit_client(tmp_path, recording_service) as client:
        response = client.get("/health")
    assert response.status_code == 200


def test_a_malformed_content_length_is_not_rejected_here(tmp_path: Path, recording_service):
    with _small_limit_client(tmp_path, recording_service) as client:
        response = client.request(
            "GET", "/health", headers={"Content-Length": "not-a-number"}
        )
    assert response.status_code == 200
