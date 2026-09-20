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

# How often an idle worker wakes on its own to re-check `_stopping`, instead
# of relying solely on a shutdown sentinel reaching it. Sized well under any
# sane grace period (seconds) while staying negligible against normal
# operation (an idle worker just re-blocks after a cheap queue.Empty).
_SHUTDOWN_POLL_SECONDS = 0.1


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
        # Validated, not coerced. `max(1, ...)` hid a misconfiguration:
        # queue.Queue(maxsize=0) in particular means *unbounded*, which
        # would silently turn the 503 backpressure into an OOM. Settings
        # rejects these at startup; this is the same check one layer in,
        # for the pool's direct callers.
        if threads < 1:
            raise ValueError(f"threads must be at least 1, got {threads}")
        if queue_max < 1:
            raise ValueError(f"queue_max must be at least 1, got {queue_max}")
        self._thread_count = threads
        # queue.Queue, not asyncio.Queue: the consumers are OS threads.
        self._queue: queue.Queue = queue.Queue(maxsize=queue_max)
        self._threads: list[threading.Thread] = []
        self._in_flight = 0
        self._in_flight_lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        # Set by stop() before anything else, so a worker that reaches the
        # top of its loop after that point exits instead of starting
        # another conversion. Never cleared: a stopped pool stays stopped.
        self._stopping = threading.Event()

    def start(self) -> None:
        for index in range(self._thread_count):
            thread = threading.Thread(
                target=self._run, name=f"docling-worker-{index}", daemon=True
            )
            thread.start()
            self._threads.append(thread)
        logger.info("Started %d conversion worker(s)", self._thread_count)

    def stop(self, grace_seconds: float) -> None:
        """Stop accepting new work; give in-flight conversions a grace period.

        The ordering here is the whole fix, and each step is load-bearing.

        `_stopping` is set first. The spec's contract is "stop accepting
        new work, allow in-flight conversions a grace period to finish",
        and appending sentinels to the tail did the opposite: a worker
        kept taking queued jobs until it finally reached a sentinel, so
        shutdown drained the entire backlog and started brand-new
        conversions inside the grace window. With the flag set up front, a
        worker finishes the job it is on and then exits.

        The deadline is computed before the puts, not after, so
        `grace_seconds` bounds the whole of stop() rather than only the
        part following them.

        Sentinels are offered with a single best-effort `put_nowait` pass,
        same as before -- but they are now a latency shortcut, not the
        correctness mechanism. A one-shot pass here previously *was* the
        only way to wake a worker blocked on an empty queue, and when
        `queue_max < len(self._threads)` the queue could only ever hold
        `queue_max` sentinels: the rest raised `queue.Full` with nothing
        left to retry them, and those workers sat on their blocking
        `get()` until the join deadline, stranding them until this
        process's next restart. `_run()` now bounds its `get()` with
        `_SHUTDOWN_POLL_SECONDS` and rechecks `_stopping` on every timeout,
        so a worker notices shutdown on its own within one poll interval
        regardless of whether a sentinel ever reached it or how the queue
        is sized relative to the thread count. A sentinel that does not
        fit is genuinely fine now: a full queue means every worker either
        has an item waiting (and now *any* item makes it exit, per the
        `_stopping` check below) or is about to time out its `get()` and
        check `_stopping` directly.

        Lock note: this method touches the queue's lock alone and never
        `_in_flight_lock`, so it cannot invert the one-directional
        `_in_flight_lock` -> queue-lock ordering the rest of the class
        depends on.
        """
        self._stopping.set()
        deadline = time.monotonic() + grace_seconds
        for _ in self._threads:
            try:
                self._queue.put_nowait(_SHUTDOWN)
            except queue.Full:
                # No retry needed: a worker that never sees a sentinel
                # still notices `_stopping` on its next `get()` timeout,
                # within _SHUTDOWN_POLL_SECONDS.
                logger.debug("Queue full while offering a shutdown sentinel")
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        self._threads.clear()

    def enqueue(self, job_id: str) -> None:
        # The queue mutation and the idle-state update must be atomic with
        # respect to _leave_job()'s read of queue.empty(): if they were two
        # unguarded statements (clear idle, *then* put), a concurrent
        # _leave_job() landing in between could see the queue as still
        # empty and incorrectly set idle back to True while this item sits
        # unprocessed. Holding _in_flight_lock across the whole thing closes
        # that window entirely -- no item can become visible in the queue
        # while a concurrent idle recompute is possible.
        #
        # Lock ordering: _in_flight_lock is always acquired before touching
        # the queue's internal lock (via put_nowait/empty), never the other
        # way around -- _run()'s queue.get() releases the queue's lock
        # before _enter_job() ever asks for _in_flight_lock. Keep it that
        # way; acquiring in the reverse order anywhere would risk deadlock.
        with self._in_flight_lock:
            self._idle.clear()
            try:
                self._queue.put_nowait(job_id)
            except queue.Full as exc:
                # The put never happened; nothing became visible to a
                # worker, so re-derive the true idle state instead of
                # leaving it stale-cleared.
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
            # Bounded, not `self._queue.get()`: a plain blocking get() only
            # ever wakes for an item actually landing in the queue, so a
            # worker parked on an empty queue depended entirely on a
            # shutdown sentinel reaching it. When queue_max is smaller than
            # the thread count that could not be guaranteed (see stop()'s
            # docstring) and the worker was stranded until the join
            # deadline. Timing out here instead means every worker rechecks
            # `_stopping` on its own at least every _SHUTDOWN_POLL_SECONDS,
            # independent of queue capacity or whether a sentinel arrived.
            try:
                item = self._queue.get(timeout=_SHUTDOWN_POLL_SECONDS)
            except queue.Empty:
                if self._stopping.is_set():
                    return
                continue

            if item is _SHUTDOWN:
                return

            if self._stopping.is_set():
                # Shutdown began while this job sat in the queue, so it
                # must not be started now -- the grace period belongs to
                # work already running. The row stays `queued`, and
                # recover_interrupted() re-enqueues it on the next start,
                # which is exactly what happens to anything still in the
                # queue at shutdown.
                logger.info(
                    "Shutting down; job %s stays queued for the next start", item
                )
                # Recompute rather than leave idle stale-cleared. Takes
                # _in_flight_lock before reading the queue, the same
                # direction as everywhere else in this class.
                with self._in_flight_lock:
                    self._update_idle_locked()
                return

            self._enter_job()
            try:
                self._process(item)
            except Exception:
                # Nothing may escape: a worker thread must outlive a bad job.
                logger.exception("Worker loop error on job %s", item)
            finally:
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
            # The exception text goes to the log, not to the job record:
            # job.error_message is served over HTTP, and docling's failures
            # routinely embed container-internal filesystem paths (a missing
            # EasyOCR weight reports its full path under MODELS_DIR). The
            # type alone is enough for a caller to tell failures apart.
            logger.exception("Job %s raised an unexpected error", job_id)
            self._safe_mark_failed(
                job_id,
                "internal_error",
                f"The conversion failed unexpectedly ({type(exc).__name__}). "
                "See the server logs for details.",
            )
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
            # Routed through the shared definition (rather than an inline
            # self._idle.clear()) so idle has exactly one definition: an
            # in-flight count above zero always makes _update_idle_locked()
            # clear it anyway, but this keeps that fact in one place.
            self._update_idle_locked()

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
