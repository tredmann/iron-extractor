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


def test_idle_is_cleared_before_the_item_becomes_visible(harness):
    """enqueue() must clear _idle before the item is visible to a worker.

    Otherwise a concurrent wait_idle() caller can observe a false "idle"
    between the put and a worker picking the item up. This asserts the
    happens-before ordering directly (deterministic) rather than timing it.

    Every put is recorded rather than just the last one: stop() now offers
    its shutdown sentinels through put_nowait too (it used to use a
    blocking put), and a sentinel legitimately goes in while the pool is
    idle. Only the real job's put is the one under test.
    """
    store, paths = harness
    service = RecordingService()
    pool = WorkerPool(store, service, paths, threads=1, queue_max=4)
    pool.start()
    try:
        assert pool.wait_idle(timeout=5)  # starts genuinely idle

        original_put_nowait = pool._queue.put_nowait
        observed: list[tuple] = []

        def spy_put_nowait(item):
            observed.append((item, not pool._idle.is_set()))
            return original_put_nowait(item)

        pool._queue.put_nowait = spy_put_nowait

        _seed_job(store, paths, "job-1")
        pool.enqueue("job-1")
        assert pool.wait_idle(timeout=5)
    finally:
        pool.stop(grace_seconds=5)

    assert ("job-1", True) in observed


class _RaisingMarkRunningStore:
    """Wraps a real JobStore but makes mark_running raise.

    Used to prove that a store-layer failure between "job resolved" and
    "service invoked" still lands the job in a terminal state, instead of
    stranding it as RUNNING forever.
    """

    def __init__(self, store: JobStore) -> None:
        self._store = store

    def get(self, job_id):
        return self._store.get(job_id)

    def mark_running(self, job_id):
        raise RuntimeError("store is broken")

    def mark_succeeded(self, job_id, manifest):
        return self._store.mark_succeeded(job_id, manifest)

    def mark_failed(self, job_id, error_type, error_message):
        return self._store.mark_failed(job_id, error_type, error_message)


def test_store_failure_during_mark_running_still_fails_the_job(harness):
    store, paths = harness
    wrapped_store = _RaisingMarkRunningStore(store)
    service = RecordingService()
    pool = WorkerPool(wrapped_store, service, paths, threads=1, queue_max=4)
    pool.start()
    try:
        _seed_job(store, paths, "job-1")
        pool.enqueue("job-1")
        assert pool.wait_idle(timeout=5)
        assert pool.worker_count == 1
    finally:
        pool.stop(grace_seconds=5)

    job = store.get("job-1")
    assert job.status is JobStatus.FAILED
    assert job.error_type == "internal_error"
    assert service.calls == []


class _ThoroughlyBrokenStore:
    """Even mark_failed raises. The worker must survive and move on."""

    def __init__(self, store: JobStore) -> None:
        self._store = store

    def get(self, job_id):
        return self._store.get(job_id)

    def mark_running(self, job_id):
        raise RuntimeError("mark_running is broken")

    def mark_succeeded(self, job_id, manifest):
        raise AssertionError("should not be called")

    def mark_failed(self, job_id, error_type, error_message):
        raise RuntimeError("mark_failed is also broken")


def test_worker_survives_when_mark_failed_itself_raises(harness):
    store, paths = harness
    wrapped_store = _ThoroughlyBrokenStore(store)
    service = RecordingService()
    pool = WorkerPool(wrapped_store, service, paths, threads=1, queue_max=4)
    pool.start()
    try:
        _seed_job(store, paths, "job-1")
        pool.enqueue("job-1")
        assert pool.wait_idle(timeout=5)
        assert pool.worker_count == 1
    finally:
        pool.stop(grace_seconds=5)


def test_enqueue_prevents_a_concurrent_leave_job_from_falsely_reporting_idle(harness):
    """Regression test for the enqueue/_leave_job race left open by the first
    ordering fix.

    Round 1 made enqueue() clear _idle *before* calling put_nowait, as two
    separate, unguarded statements. That closed the original window but
    opened an equivalent one: a concurrent _leave_job() (the last in-flight
    job finishing) landing between those two statements reads queue.empty()
    as still True (the new item hasn't been put yet) and incorrectly sets
    idle back to True, even though the item about to be enqueued is still
    pending.

    This is reproduced deterministically without real thread scheduling: a
    non-blocking, non-reentrant self-acquire of _in_flight_lock from inside
    a monkeypatched put_nowait either succeeds instantly (enqueue is NOT
    holding the lock across the mutation -- the bug) or fails instantly
    (enqueue IS holding it for the whole operation -- the fix). No timing
    assumptions, no sleeps, no real second thread required.
    """
    store, paths = harness
    service = RecordingService()
    pool = WorkerPool(store, service, paths, threads=1, queue_max=4)
    # Deliberately do not start() the pool: this test drives _enter_job
    # directly to simulate "one job already in flight" and needs no real
    # worker thread to reproduce the interleaving.
    _seed_job(store, paths, "job-1")

    pool._enter_job()  # simulate: job A already in flight, queue empty

    original_put_nowait = pool._queue.put_nowait

    def racing_put_nowait(item):
        # Simulate a concurrent _leave_job() (job A finishing) landing
        # exactly between "idle cleared" and "item visible in the queue".
        if pool._in_flight_lock.acquire(blocking=False):
            try:
                pool._in_flight -= 1
                pool._update_idle_locked()
            finally:
                pool._in_flight_lock.release()
        return original_put_nowait(item)

    pool._queue.put_nowait = racing_put_nowait

    pool.enqueue("job-1")

    assert pool._idle.is_set() is False


def test_stop_returns_promptly_when_the_queue_is_full(harness):
    """`stop()` used to push sentinels with a blocking `put`. On a full
    queue that waits for a worker to take an item -- and workers take
    queued jobs, not sentinels -- so at SIGTERM with a busy worker it
    blocked well past any grace period, on the event loop's thread."""
    import time

    store, paths = harness
    service = RecordingService()
    service.released.clear()  # pin the worker inside a conversion
    pool = WorkerPool(store, service, paths, threads=1, queue_max=1)
    pool.start()
    try:
        for job_id in ("job-1", "job-2"):
            _seed_job(store, paths, job_id)
        pool.enqueue("job-1")
        assert service.started.wait(timeout=5)  # worker is busy; queue empty
        pool.enqueue("job-2")  # queue is now at capacity

        began = time.monotonic()
        pool.stop(grace_seconds=0.0)
        elapsed = time.monotonic() - began
    finally:
        service.released.set()

    assert elapsed < 1.0, f"stop() blocked for {elapsed:.1f}s on a full queue"


@pytest.mark.parametrize("threads,queue_max", [(4, 1), (8, 2), (2, 1)])
def test_stop_returns_promptly_when_queue_is_smaller_than_thread_count(
    harness, threads, queue_max
):
    """Regression test: stop() used to offer each worker a single
    `put_nowait(_SHUTDOWN)` attempt. When `queue_max < threads`, the queue
    could only ever hold `queue_max` sentinels; the rest raised
    `queue.Full` with no retry, and those workers -- parked on a blocking
    `get()` on an otherwise empty, idle queue -- were never woken. They sat
    there until the join deadline (consuming the *entire* grace period)
    and were then silently dropped from `self._threads`, stranded for
    good.

    This pins an idle pool (every worker genuinely parked on an empty
    queue, not mid-conversion) at exactly the sizes the regression was
    measured at, and asserts stop() returns promptly with every worker
    thread actually dead -- not just absent from the pool's own bookkeeping,
    which `stop()` clears unconditionally regardless of whether join
    succeeded.
    """
    import time

    store, paths = harness
    service = RecordingService()
    pool = WorkerPool(store, service, paths, threads=threads, queue_max=queue_max)
    pool.start()
    assert pool.wait_idle(timeout=5)  # every worker parked on get(), queue empty

    threads_before = list(pool._threads)
    began = time.monotonic()
    pool.stop(grace_seconds=5.0)
    elapsed = time.monotonic() - began

    assert elapsed < 1.0, (
        f"stop() took {elapsed:.2f}s for threads={threads}, queue_max={queue_max}"
        " -- a worker was likely stranded until the join deadline"
    )
    assert all(not t.is_alive() for t in threads_before), (
        f"threads={threads}, queue_max={queue_max}: a worker thread is still"
        " alive after stop() returned"
    )


def test_stop_does_not_start_queued_work(harness):
    """The grace period is for conversions already running. Sentinels at
    the tail of the queue meant shutdown drained the whole backlog first,
    starting brand-new conversions during the window meant for finishing
    old ones."""
    store, paths = harness
    service = RecordingService()
    service.released.clear()
    pool = WorkerPool(store, service, paths, threads=1, queue_max=4)
    pool.start()
    try:
        for job_id in ("job-1", "job-2", "job-3"):
            _seed_job(store, paths, job_id)
        pool.enqueue("job-1")
        assert service.started.wait(timeout=5)
        pool.enqueue("job-2")
        pool.enqueue("job-3")

        # stop() must run concurrently with the held job: it blocks in
        # join() until the worker finishes. Waiting on _stopping makes the
        # handover deterministic -- no sleeps, no scheduling assumptions.
        stopper = threading.Thread(target=pool.stop, args=(5.0,))
        stopper.start()
        assert pool._stopping.wait(timeout=5)
    finally:
        service.released.set()  # job-1 may now finish

    stopper.join(timeout=10)
    assert not stopper.is_alive()

    assert [call[0] for call in service.calls] == ["job-1"]
    assert store.get("job-2").status is JobStatus.QUEUED
    assert store.get("job-3").status is JobStatus.QUEUED


def test_a_worker_holding_a_job_when_stop_begins_still_finishes_it(harness):
    """The other half of the contract: in-flight work is allowed to
    complete within the grace period rather than being abandoned."""
    store, paths = harness
    service = RecordingService()
    service.released.clear()
    pool = WorkerPool(store, service, paths, threads=1, queue_max=4)
    pool.start()
    try:
        _seed_job(store, paths, "job-1")
        pool.enqueue("job-1")
        assert service.started.wait(timeout=5)

        stopper = threading.Thread(target=pool.stop, args=(5.0,))
        stopper.start()
        assert pool._stopping.wait(timeout=5)
    finally:
        service.released.set()

    stopper.join(timeout=10)
    assert store.get("job-1").status is JobStatus.SUCCEEDED


def test_pool_rejects_a_non_positive_size(harness):
    store, paths = harness
    with pytest.raises(ValueError):
        WorkerPool(store, RecordingService(), paths, threads=0, queue_max=4)
    with pytest.raises(ValueError):
        WorkerPool(store, RecordingService(), paths, threads=1, queue_max=0)
