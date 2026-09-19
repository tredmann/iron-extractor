def test_health_is_unauthenticated_and_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_unknown_route_uses_error_envelope(client):
    response = client.get("/nope")
    assert response.status_code == 404
    assert response.json() == {"error": {"code": "not_found", "message": "Not Found"}}


def test_models_loaded_reflects_the_converter_provider(tmp_path, monkeypatch):
    """`models_loaded` was a constant fixed at construction time (`service
    is None`), so it reported `true` before a single weight had been read
    and the container healthcheck went green even if /opt/models was
    unreadable. It now asks the provider whether it has built anything."""
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.conversion.converters import ConverterProvider
    from app.main import create_app

    class FakeConverter:
        """Builds fine (so the provider counts as loaded) but cannot convert."""

        def convert(self, source):
            raise RuntimeError("no weights in this test")

    monkeypatch.setattr(
        ConverterProvider, "_build_converter", lambda self, options: FakeConverter()
    )

    settings = Settings(api_keys="k", data_dir=tmp_path, worker_threads=1, queue_max=4)
    app = create_app(settings=settings)  # the real provider, no docling import
    with TestClient(app) as client:
        assert client.get("/health").json()["models_loaded"] is False

        client.post(
            "/v1/documents",
            headers={"X-API-Key": "k"},
            files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")},
        )
        assert app.state.pool.wait_idle(timeout=5)

        assert client.get("/health").json()["models_loaded"] is True


def test_models_loaded_is_false_for_a_substituted_service(client):
    assert client.get("/health").json()["models_loaded"] is False
