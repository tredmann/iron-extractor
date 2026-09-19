import pytest

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


def test_startup_failure_cleans_up_pool_and_store(settings: Settings, recording_service, monkeypatch):
    """If startup fails partway through, already-started resources must be
    torn down (worker threads stopped, store closed) before the exception
    propagates -- otherwise a bad startup leaks an open sqlite connection
    and live daemon threads.
    """

    def _boom(self):
        raise RuntimeError("recover_interrupted exploded")

    monkeypatch.setattr(JobStore, "recover_interrupted", _boom)

    app = create_app(settings=settings, service=recording_service)

    with pytest.raises(RuntimeError, match="recover_interrupted exploded"):
        with TestClient(app):
            pass

    store = app.state.store
    pool = app.state.pool

    # The store must have been closed: its connection is gone, and any
    # further use raises the same error connect() failing to happen would.
    assert store._conn is None

    # No worker thread should still be running.
    assert pool.worker_count == 0
