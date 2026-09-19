from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_health_is_unauthenticated_and_ok(tmp_path: Path):
    app = create_app(Settings(api_keys="secret", data_dir=tmp_path))
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_unknown_route_uses_error_envelope(tmp_path: Path):
    app = create_app(Settings(api_keys="secret", data_dir=tmp_path))
    with TestClient(app) as client:
        response = client.get("/nope")
    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "not_found", "message": "Not Found"}
    }
