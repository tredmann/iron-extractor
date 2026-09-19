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


def _client_with_keys(tmp_path: Path, api_keys: str) -> TestClient:
    app = create_app(Settings(api_keys=api_keys, data_dir=tmp_path))

    @app.get("/protected", dependencies=[Depends(require_api_key)])
    async def protected() -> dict:
        return {"ok": True}

    return TestClient(app)


def test_blank_key_header_is_rejected(tmp_path: Path):
    """A blank header must not slip past as "present". APIKeyHeader yields
    None for an empty value, so this lands on the missing-header branch --
    pinned here because a silent regression would be catastrophic."""
    with _client(tmp_path) as client:
        response = client.get("/protected", headers={"X-API-Key": ""})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_whitespace_only_key_is_rejected(tmp_path: Path):
    with _client(tmp_path) as client:
        response = client.get("/protected", headers={"X-API-Key": "   "})
    assert response.status_code == 401


def test_zero_configured_keys_accepts_nothing(tmp_path: Path):
    """`API_KEYS=""` yields an empty key list, and `any()` over it is
    False -- so the server accepts nothing rather than everything."""
    with _client_with_keys(tmp_path, "") as client:
        assert client.get("/protected").status_code == 401
        assert client.get("/protected", headers={"X-API-Key": ""}).status_code == 401
        assert client.get("/protected", headers={"X-API-Key": "anything"}).status_code == 401


def test_keys_that_are_only_separators_accept_nothing(tmp_path: Path):
    with _client_with_keys(tmp_path, " , ,, ") as client:
        assert client.get("/protected", headers={"X-API-Key": ""}).status_code == 401
        assert client.get("/protected", headers={"X-API-Key": " "}).status_code == 401
        assert client.get("/protected", headers={"X-API-Key": ","}).status_code == 401


def test_rejections_are_logged_with_the_client_host(tmp_path: Path, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="app.api.deps"):
        with _client(tmp_path) as client:
            client.get("/protected", headers={"X-API-Key": "nope"})

    assert any(
        "Rejected GET /protected" in record.getMessage()
        and "unrecognized key" in record.getMessage()
        for record in caplog.records
    )
    assert not any("nope" in record.getMessage() for record in caplog.records)
