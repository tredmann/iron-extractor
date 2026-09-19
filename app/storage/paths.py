import shutil
from pathlib import Path


def sanitize_filename(raw: str) -> str:
    """Reduce an uploaded filename to a safe basename."""
    cleaned = raw.replace("\x00", "").strip()
    name = Path(cleaned).name.strip()
    if not name or name in {".", ".."}:
        return "upload"
    return name


class JobPaths:
    """Resolves every on-disk location belonging to a job."""

    def __init__(self, data_dir: Path) -> None:
        self.jobs_root = Path(data_dir) / "jobs"

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_root / job_id

    def input_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "input"

    def output_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "output"

    def tables_dir(self, job_id: str) -> Path:
        return self.output_dir(job_id) / "tables"

    def images_dir(self, job_id: str) -> Path:
        return self.output_dir(job_id) / "images"

    def markdown_file(self, job_id: str) -> Path:
        return self.output_dir(job_id) / "document.md"

    def document_file(self, job_id: str) -> Path:
        return self.output_dir(job_id) / "document.json"

    def table_file(self, job_id: str, index: int, fmt: str) -> Path:
        return self.tables_dir(job_id) / f"table-{index:03d}.{fmt}"

    def image_file(self, job_id: str, name: str) -> Path:
        """Resolve an image by name, refusing anything that escapes the job."""
        images_dir = self.images_dir(job_id)
        candidate = (images_dir / name).resolve()
        if candidate.parent != images_dir.resolve():
            raise ValueError(f"Illegal image name: {name!r}")
        return candidate

    def input_file(self, job_id: str, filename: str) -> Path:
        return self.input_dir(job_id) / sanitize_filename(filename)

    def create(self, job_id: str) -> None:
        for directory in (
            self.input_dir(job_id),
            self.output_dir(job_id),
            self.tables_dir(job_id),
            self.images_dir(job_id),
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def remove(self, job_id: str) -> None:
        """Delete a job's directory, refusing anything that is not one.

        The containment check is load-bearing, not decorative: `job_id`
        reaches here straight from a `{job_id}` path parameter, and uvicorn
        percent-decodes the path before Starlette routes it, so `..` and `.`
        both match the `str` converter's `[^/]+` and arrive as literal ids.
        Unguarded, `rmtree(jobs_root / "..")` is `rmtree(DATA_DIR)` -- the
        database, its WAL and every other job. Same guard as image_file().
        """
        target = self.job_dir(job_id)
        if target.resolve().parent != self.jobs_root.resolve():
            raise ValueError(f"Illegal job id: {job_id!r}")
        shutil.rmtree(target, ignore_errors=True)
