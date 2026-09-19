import logging
import queue
import threading
import time

from app.api.schemas import ConversionOptions
from app.conversion.service import ConversionFailed
from app.jobs.store import JobStore
from app.storage.paths import JobPaths

logger = logging.getLogger(__name__)

_SHUTDOWN = object()


class QueueFull(Exception):
    """The conversion queue is at capacity."""


class WorkerPool:
    """A bounded work queue drained by a fixed set of conversion threads."""

    def __init__(
        self,
        store: JobStore,
        service,
        paths: JobPaths,
        threads: int,
        queue_max: int,
    ) -> None:
        self._store = store
        self._service = service
        self._paths = paths
        self._thread_count = max(1, threads)
        # queue.Queue, not asyncio.Queue: the consumers are OS threads.
        self._queue: queue.Queue = queue.Queue(maxsize=max(1, queue_max))
        self._threads: list[threading.Thread] = []
        self._in_flight = 0
        self._in_flight_lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()

    def start(self) -> None:
        for index in range(self._thread_count):
            thread = threading.Thread(
                target=self._run, name=f"docling-worker-{index}", daemon=True
            )
            thread.start()
            self._threads.append(thread)
        logger.info("Started %d conversion worker(s)", self._thread_count)

    def stop(self, grace_seconds: float) -> None:
        for _ in self._threads:
            self._queue.put(_SHUTDOWN)
        deadline = time.monotonic() + grace_seconds
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        self._threads.clear()

    def enqueue(self, job_id: str) -> None:
        # Clear idle *before* the item becomes visible to a consumer, so a
        # concurrent wait_idle() can never observe a false "idle" between the
        # put and a worker picking the item up.
        self._idle.clear()
        try:
            self._queue.put_nowait(job_id)
        except queue.Full as exc:
            # The put never happened; nothing became visible to a worker, so
            # re-derive the true idle state instead of leaving it stale-cleared.
            with self._in_flight_lock:
                self._update_idle_locked()
            raise QueueFull(job_id) from exc

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    @property
    def worker_count(self) -> int:
        return sum(1 for thread in self._threads if thread.is_alive())

    def wait_idle(self, timeout: float) -> bool:
        """Block until the queue is drained and nothing is in flight."""
        return self._idle.wait(timeout=timeout)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _SHUTDOWN:
                self._queue.task_done()
                return

            self._enter_job()
            try:
                self._process(item)
            except Exception:
                # Nothing may escape: a worker thread must outlive a bad job.
                logger.exception("Worker loop error on job %s", item)
            finally:
                self._queue.task_done()
                self._leave_job()

    def _process(self, job_id: str) -> None:
        job = self._store.get(job_id)
        if job is None:
            logger.warning("Job %s vanished before it could run", job_id)
            return

        input_path = self._paths.input_file(job_id, job.filename)
        if not input_path.exists():
            self._safe_mark_failed(
                job_id, "input_missing", "The uploaded file is no longer on disk."
            )
            return

        started = time.monotonic()
        try:
            # mark_running is inside the try: a store/disk failure here must
            # still land the job in a terminal (failed) state, not strand it.
            self._store.mark_running(job_id)
            manifest = self._service.run(job_id, input_path, ConversionOptions(**job.options))
        except ConversionFailed as exc:
            logger.info("Job %s failed (%s): %s", job_id, exc.error_type, exc.message)
            self._safe_mark_failed(job_id, exc.error_type, exc.message)
        except Exception as exc:
            logger.exception("Job %s raised an unexpected error", job_id)
            self._safe_mark_failed(job_id, "internal_error", f"{type(exc).__name__}: {exc}")
        else:
            self._store.mark_succeeded(job_id, manifest)
            logger.info(
                "Job %s succeeded in %.1fs (%s pages)",
                job_id,
                time.monotonic() - started,
                manifest.get("page_count"),
            )

    def _safe_mark_failed(self, job_id: str, error_type: str, message: str) -> None:
        """mark_failed, but a broken store must not take the worker down with it."""
        try:
            self._store.mark_failed(job_id, error_type, message)
        except Exception:
            logger.exception(
                "Could not record failure for job %s (error_type=%s); the store"
                " itself appears to be broken.",
                job_id,
                error_type,
            )

    def _enter_job(self) -> None:
        with self._in_flight_lock:
            self._in_flight += 1
            self._idle.clear()

    def _leave_job(self) -> None:
        with self._in_flight_lock:
            self._in_flight -= 1
            self._update_idle_locked()

    def _update_idle_locked(self) -> None:
        """Single definition of idle: no job in flight and nothing queued.

        Caller must hold _in_flight_lock.
        """
        if self._in_flight == 0 and self._queue.empty():
            self._idle.set()
        else:
            self._idle.clear()
