import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

pytest.importorskip("docling", reason="real docling not installed")

FIXTURE = Path(__file__).parent / "fixtures" / "sample.pdf"


@pytest.mark.slow
def test_real_conversion_produces_artifacts(tmp_path: Path):
    settings = Settings(
        api_keys="e2e-key",
        data_dir=tmp_path,
        worker_threads=1,
        job_timeout_seconds=600.0,
    )
    auth = {"X-API-Key": "e2e-key"}

    app = create_app(settings=settings)
    with TestClient(app) as client:
        created = client.post(
            "/v1/documents",
            headers=auth,
            files={"file": ("sample.pdf", FIXTURE.read_bytes(), "application/pdf")},
            data={"options": '{"ocr": false, "table_mode": "fast"}'},
        )
        assert created.status_code == 202
        job_id = created.json()["id"]

        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            payload = client.get(f"/v1/documents/{job_id}", headers=auth).json()
            if payload["status"] in ("succeeded", "failed"):
                break
            time.sleep(2)

        assert payload["status"] == "succeeded", payload.get("error")
        assert payload["result"]["page_count"] == 1

        markdown = client.get(f"/v1/documents/{job_id}/markdown", headers=auth)
        assert markdown.status_code == 200
        assert "Docling" in markdown.text

        document = client.get(f"/v1/documents/{job_id}/document.json", headers=auth)
        assert document.status_code == 200
        assert "texts" in document.json()

        assert client.delete(f"/v1/documents/{job_id}", headers=auth).status_code == 204
