import threading
from pathlib import Path

import pytest

from app.conversion.service import ConversionFailed
from app.jobs.models import JobStatus
from app.jobs.pool import QueueFull, WorkerPool
from app.jobs.store import JobStore
from app.storage.paths import JobPaths


class RecordingService:
    """Stands in for ConversionService; records calls, never loads a model."""

    def __init__(self, manifest=None, raises=None):
        self.manifest = manifest if manifest is not None else {"page_count": 1}
        self.raises = raises
        self.calls: list[tuple] = []
        self.released = threading.Event()
        self.released.set()
        self.started = threading.Event()

    def run(self, job_id, input_path, options):
        # Signal entry before the gate so callers can deterministically know
        # the item has left the queue (rather than racing on scheduling).
        self.started.set()
        self.released.wait(timeout=5)
        self.calls.append((job_id, input_path, options))
        if self.raises is not None:
            raise self.raises
        return self.manifest


@pytest.fixture
def harness(tmp_path: Path):
    paths = JobPaths(tmp_path)
    store = JobStore(tmp_path / "jobs.sqlite3", paths)
    store.connect()
    yield store, paths
    store.close()


def _seed_job(store: JobStore, paths: JobPaths, job_id: str) -> None:
    paths.create(job_id)
    paths.input_file(job_id, "in.pdf").write_bytes(b"%PDF-1.4")
    store.create(job_id, "in.pdf", {"ocr": True, "ocr_languages": ["en"],
                                    "table_mode": "accurate", "image_scale": 2.0})


def test_successful_job_is_marked_succeeded(harness):
    store, paths = harness
    service = RecordingService(manifest={"page_count": 7})
    pool = WorkerPool(store, service, paths, threads=1, queue_max=4)
    pool.start()
    try:
        _seed_job(store, paths, "job-1")
        pool.enqueue("job-1")
        assert pool.wait_idle(timeout=5)
    finally:
        pool.stop(grace_seconds=5)

    job = store.get("job-1")
    assert job.status is JobStatus.SUCCEEDED
    assert job.manifest == {"page_count": 7}
    assert job.started_at is not None


def test_service_receives_input_path_and_options(harness):
    store, paths = harness
    service = RecordingService()
    pool = WorkerPool(store, service, paths, threads=1, queue_max=4)
    pool.start()
    try:
        _seed_job(store, paths, "job-1")
        pool.enqueue("job-1")
        assert pool.wait_idle(timeout=5)
    finally:
        pool.stop(grace_seconds=5)

    job_id, input_path, options = service.calls[0]
    assert job_id == "job-1"
    assert input_path == paths.input_file("job-1", "in.pdf")
    assert options.ocr is True


def test_conversion_failure_is_recorded(harness):
    store, paths = harness
    service = RecordingService(raises=ConversionFailed("timeout", "too slow"))
    pool = WorkerPool(store, service, paths, threads=1, queue_max=4)
    pool.start()
    try:
        _seed_job(store, paths, "job-1")
        pool.enqueue("job-1")
        assert pool.wait_idle(timeout=5)
    finally:
        pool.stop(grace_seconds=5)

    job = store.get("job-1")
    assert job.status is JobStatus.FAILED
    assert job.error_type == "timeout"
    assert job.error_message == "too slow"


def test_unexpected_error_becomes_internal_error(harness):
    store, paths = harness
    service = RecordingService(raises=RuntimeError("boom"))
    pool = WorkerPool(store, service, paths, threads=1, queue_max=4)
    pool.start()
    try:
        _seed_job(store, paths, "job-1")
        pool.enqueue("job-1")
        assert pool.wait_idle(timeout=5)
    finally:
        pool.stop(grace_seconds=5)

    job = store.get("job-1")
    assert job.status is JobStatus.FAILED
    assert job.error_type == "internal_error"


def test_worker_survives_a_bad_job(harness):
    store, paths = harness
    service = RecordingService(raises=RuntimeError("boom"))
    pool = WorkerPool(store, service, paths, threads=1, queue_max=4)
    pool.start()
    try:
        _seed_job(store, paths, "job-1")
        _seed_job(store, paths, "job-2")
        pool.enqueue("job-1")
        pool.enqueue("job-2")
        assert pool.wait_idle(timeout=5)
        assert pool.worker_count == 1
    finally:
        pool.stop(grace_seconds=5)

    assert store.get("job-2").status is JobStatus.FAILED
    assert len(service.calls) == 2


def test_enqueue_raises_when_the_queue_is_full(harness):
    store, paths = harness
    service = RecordingService()
    service.released.clear()  # hold the worker so the queue backs up
    pool = WorkerPool(store, service, paths, threads=1, queue_max=1)
    pool.start()
    try:
        for job_id in ("job-1", "job-2", "job-3"):
            _seed_job(store, paths, job_id)
        pool.enqueue("job-1")
        # Wait until the worker has actually dequeued job-1 (entered the
        # service call) before relying on the queue being at capacity again;
        # otherwise this races the worker thread's scheduling.
        assert service.started.wait(timeout=5)
        pool.enqueue("job-2")
        with pytest.raises(QueueFull):
            pool.enqueue("job-3")
        assert pool.queue_depth >= 1
    finally:
        service.released.set()
        pool.stop(grace_seconds=5)


def test_missing_job_row_is_skipped(harness):
    store, paths = harness
    service = RecordingService()
    pool = WorkerPool(store, service, paths, threads=1, queue_max=4)
    pool.start()
    try:
        pool.enqueue("ghost")
        assert pool.wait_idle(timeout=5)
    finally:
        pool.stop(grace_seconds=5)

    assert service.calls == []


def test_missing_input_file_fails_the_job(harness):
    store, paths = harness
    service = RecordingService()
    pool = WorkerPool(store, service, paths, threads=1, queue_max=4)
    pool.start()
    try:
        paths.create("job-1")
        store.create("job-1", "in.pdf", {})
        pool.enqueue("job-1")
        assert pool.wait_idle(timeout=5)
    finally:
        pool.stop(grace_seconds=5)

    job = store.get("job-1")
    assert job.status is JobStatus.FAILED
    assert job.error_type == "input_missing"
    assert service.calls == []
