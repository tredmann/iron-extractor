import threading
from pathlib import Path

import pytest

from app.jobs.models import JobStatus
from app.jobs.store import JobStore
from app.storage.paths import JobPaths


@pytest.fixture
def store(tmp_path: Path):
    paths = JobPaths(tmp_path)
    store = JobStore(tmp_path / "jobs.sqlite3", paths)
    store.connect()
    yield store
    store.close()


def test_create_then_get_roundtrips(store: JobStore):
    created = store.create("job-1", "report.pdf", {"ocr": True})
    assert created.status is JobStatus.QUEUED
    fetched = store.get("job-1")
    assert fetched is not None
    assert fetched.filename == "report.pdf"
    assert fetched.options == {"ocr": True}
    assert fetched.created_at
    assert fetched.started_at is None
    assert fetched.manifest is None


def test_get_unknown_returns_none(store: JobStore):
    assert store.get("missing") is None


def test_status_transitions(store: JobStore):
    store.create("job-1", "a.pdf", {})
    store.mark_running("job-1")
    running = store.get("job-1")
    assert running.status is JobStatus.RUNNING
    assert running.started_at is not None

    store.mark_succeeded("job-1", {"page_count": 3})
    done = store.get("job-1")
    assert done.status is JobStatus.SUCCEEDED
    assert done.manifest == {"page_count": 3}
    assert done.finished_at is not None


def test_mark_failed_records_error(store: JobStore):
    store.create("job-1", "a.pdf", {})
    store.mark_failed("job-1", "timeout", "took too long")
    failed = store.get("job-1")
    assert failed.status is JobStatus.FAILED
    assert failed.error_type == "timeout"
    assert failed.error_message == "took too long"
    assert failed.finished_at is not None


def test_delete_removes_row_and_directory(tmp_path: Path, store: JobStore):
    paths = JobPaths(tmp_path)
    paths.create("job-1")
    store.create("job-1", "a.pdf", {})

    assert store.delete("job-1") is True
    assert store.get("job-1") is None
    assert not paths.job_dir("job-1").exists()


def test_delete_unknown_returns_false(store: JobStore):
    assert store.delete("missing") is False


def test_recover_interrupted(store: JobStore):
    store.create("queued-job", "a.pdf", {})
    store.create("running-job", "b.pdf", {})
    store.mark_running("running-job")
    store.create("done-job", "c.pdf", {})
    store.mark_succeeded("done-job", {})

    requeued = store.recover_interrupted()

    assert requeued == ["queued-job"]
    interrupted = store.get("running-job")
    assert interrupted.status is JobStatus.FAILED
    assert interrupted.error_type == "interrupted"
    assert store.get("done-job").status is JobStatus.SUCCEEDED


def test_survives_reopen(tmp_path: Path):
    paths = JobPaths(tmp_path)
    first = JobStore(tmp_path / "jobs.sqlite3", paths)
    first.connect()
    first.create("job-1", "a.pdf", {})
    first.close()

    second = JobStore(tmp_path / "jobs.sqlite3", paths)
    second.connect()
    assert second.get("job-1") is not None
    second.close()


def test_concurrent_writes_from_multiple_threads(store: JobStore):
    """Several worker threads hammer the same connection concurrently.

    Exercises the check_same_thread=False + lock combination for real:
    each thread creates its own job, transitions it through running to
    succeeded, and reads it back mid-flight, all racing against the other
    threads' writes on the same connection. A threading.Barrier forces
    every thread to start at once instead of relying on incidental
    scheduling, so the test deterministically exercises contention rather
    than hoping for it.
    """
    job_count = 8
    job_ids = [f"job-{i}" for i in range(job_count)]
    errors: list[BaseException] = []
    barrier = threading.Barrier(job_count)

    def worker(job_id: str) -> None:
        try:
            barrier.wait()
            store.create(job_id, f"{job_id}.pdf", {})
            store.mark_running(job_id)
            store.get(job_id)
            store.mark_succeeded(job_id, {"job_id": job_id})
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(job_id,)) for job_id in job_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    for job_id in job_ids:
        job = store.get(job_id)
        assert job is not None
        assert job.status is JobStatus.SUCCEEDED
        assert job.manifest == {"job_id": job_id}
