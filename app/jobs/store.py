import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.jobs.models import Job, JobStatus
from app.storage.paths import JobPaths

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    status        TEXT NOT NULL,
    filename      TEXT NOT NULL,
    options       TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    error_type    TEXT,
    error_message TEXT,
    manifest      TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs (status);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    """SQLite-backed job metadata. Safe to call from worker threads."""

    def __init__(self, db_path: Path, paths: JobPaths) -> None:
        self._db_path = Path(db_path)
        self._paths = paths
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False because worker threads write; every write is
        # serialized by self._lock.
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @property
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("JobStore.connect() has not been called")
        return self._conn

    def create(self, job_id: str, filename: str, options: dict) -> Job:
        job = Job(
            id=job_id,
            status=JobStatus.QUEUED,
            filename=filename,
            options=options,
            created_at=_now(),
        )
        with self._lock:
            self._db.execute(
                "INSERT INTO jobs (id, status, filename, options, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (job.id, job.status.value, job.filename, json.dumps(job.options), job.created_at),
            )
            self._db.commit()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row is not None else None

    def mark_running(self, job_id: str) -> None:
        self._update(
            "UPDATE jobs SET status = ?, started_at = ? WHERE id = ?",
            (JobStatus.RUNNING.value, _now(), job_id),
        )

    def mark_succeeded(self, job_id: str, manifest: dict) -> None:
        self._update(
            "UPDATE jobs SET status = ?, finished_at = ?, manifest = ? WHERE id = ?",
            (JobStatus.SUCCEEDED.value, _now(), json.dumps(manifest), job_id),
        )

    def mark_failed(self, job_id: str, error_type: str, error_message: str) -> None:
        self._update(
            "UPDATE jobs SET status = ?, finished_at = ?, error_type = ?, error_message = ?"
            " WHERE id = ?",
            (JobStatus.FAILED.value, _now(), error_type, error_message, job_id),
        )

    def delete(self, job_id: str) -> bool:
        with self._lock:
            cursor = self._db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            self._db.commit()
            deleted = cursor.rowcount > 0
        self._paths.remove(job_id)
        job_dir = self._paths.job_dir(job_id)
        if job_dir.exists():
            logger.warning(
                "Failed to remove job directory for %s at %s; it may need manual cleanup.",
                job_id,
                job_dir,
            )
        return deleted

    def recover_interrupted(self) -> list[str]:
        """Fail jobs abandoned mid-run; return queued job ids to re-enqueue."""
        with self._lock:
            self._db.execute(
                "UPDATE jobs SET status = ?, finished_at = ?, error_type = ?,"
                " error_message = ? WHERE status = ?",
                (
                    JobStatus.FAILED.value,
                    _now(),
                    "interrupted",
                    "The service restarted while this job was running.",
                    JobStatus.RUNNING.value,
                ),
            )
            rows = self._db.execute(
                "SELECT id FROM jobs WHERE status = ? ORDER BY created_at",
                (JobStatus.QUEUED.value,),
            ).fetchall()
            self._db.commit()
        return [row["id"] for row in rows]

    def _update(self, sql: str, params: tuple) -> None:
        with self._lock:
            self._db.execute(sql, params)
            self._db.commit()


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        status=JobStatus(row["status"]),
        filename=row["filename"],
        options=json.loads(row["options"]),
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        error_type=row["error_type"],
        error_message=row["error_message"],
        manifest=json.loads(row["manifest"]) if row["manifest"] else None,
    )
