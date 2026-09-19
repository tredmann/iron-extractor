from app.config import Settings
from app.jobs.models import JobStatus
from app.jobs.store import JobStore
from app.main import create_app
from app.storage.paths import JobPaths
from fastapi.testclient import TestClient


def test_health_reports_pool_state(client):
    payload = client.get("/health").json()
    assert payload["status"] == "ok"
    assert payload["workers"] == 1
    assert payload["queue_depth"] == 0
    assert payload["models_loaded"] is False


def test_startup_fails_interrupted_jobs(settings: Settings, recording_service):
    paths = JobPaths(settings.data_dir)
    seed = JobStore(settings.db_path, paths)
    seed.connect()
    seed.create("stuck", "a.pdf", {})
    seed.mark_running("stuck")
    seed.close()

    app = create_app(settings=settings, service=recording_service)
    with TestClient(app):
        pass

    check = JobStore(settings.db_path, paths)
    check.connect()
    job = check.get("stuck")
    check.close()

    assert job.status is JobStatus.FAILED
    assert job.error_type == "interrupted"


def test_startup_requeues_queued_jobs(settings: Settings, recording_service):
    paths = JobPaths(settings.data_dir)
    paths.create("pending")
    paths.input_file("pending", "a.pdf").write_bytes(b"%PDF-1.4")

    seed = JobStore(settings.db_path, paths)
    seed.connect()
    seed.create("pending", "a.pdf", {})
    seed.close()

    app = create_app(settings=settings, service=recording_service)
    with TestClient(app) as test_client:
        assert test_client.app.state.pool.wait_idle(timeout=5)

    assert recording_service.calls[0][0] == "pending"
