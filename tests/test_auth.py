from pathlib import Path

from fastapi import Depends
from fastapi.testclient import TestClient

from app.api.deps import require_api_key
from app.config import Settings
from app.main import create_app


def _client(tmp_path: Path) -> TestClient:
    app = create_app(Settings(api_keys="key-one,key-two", data_dir=tmp_path))

    @app.get("/protected", dependencies=[Depends(require_api_key)])
    async def protected() -> dict:
        return {"ok": True}

    return TestClient(app)


def test_valid_key_is_accepted(tmp_path: Path):
    with _client(tmp_path) as client:
        response = client.get("/protected", headers={"X-API-Key": "key-two"})
    assert response.status_code == 200


def test_missing_key_is_rejected(tmp_path: Path):
    with _client(tmp_path) as client:
        response = client.get("/protected")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_wrong_key_is_rejected(tmp_path: Path):
    with _client(tmp_path) as client:
        response = client.get("/protected", headers={"X-API-Key": "nope"})
    assert response.status_code == 401


def test_health_stays_open(tmp_path: Path):
    with _client(tmp_path) as client:
        assert client.get("/health").status_code == 200
