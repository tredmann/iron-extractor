from pathlib import Path

import pytest

from app.storage.paths import JobPaths, sanitize_filename


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("../../etc/passwd", "passwd"),
        ("/absolute/path/doc.pdf", "doc.pdf"),
        ("", "upload"),
        ("   ", "upload"),
        ("..", "upload"),
        ("with\x00null.pdf", "withnull.pdf"),
    ],
)
def test_sanitize_filename(raw: str, expected: str):
    assert sanitize_filename(raw) == expected


def test_create_makes_the_directory_tree(tmp_path: Path):
    paths = JobPaths(tmp_path)
    paths.create("job-1")
    assert paths.input_dir("job-1").is_dir()
    assert paths.tables_dir("job-1").is_dir()
    assert paths.images_dir("job-1").is_dir()


def test_remove_deletes_everything(tmp_path: Path):
    paths = JobPaths(tmp_path)
    paths.create("job-1")
    paths.markdown_file("job-1").write_text("hello")
    paths.remove("job-1")
    assert not paths.job_dir("job-1").exists()


def test_remove_is_idempotent(tmp_path: Path):
    JobPaths(tmp_path).remove("never-existed")


@pytest.mark.parametrize("job_id", ["..", ".", "../..", "../other"])
def test_remove_rejects_ids_that_escape_the_jobs_root(tmp_path: Path, job_id: str):
    """`..` resolves to DATA_DIR and `.` to the jobs root itself; rmtree on
    either destroys the database and every other job. Both arrive here
    verbatim from a `{job_id}` path parameter, so the guard is the only
    thing standing between a percent-encoded DELETE and the whole volume."""
    paths = JobPaths(tmp_path)
    paths.create("job-1")
    sentinel = tmp_path / "jobs.sqlite3"
    sentinel.write_text("db")

    with pytest.raises(ValueError):
        paths.remove(job_id)

    assert sentinel.exists()
    assert paths.jobs_root.is_dir()
    assert paths.job_dir("job-1").is_dir()


@pytest.mark.parametrize("job_id", ["job-1", "0f2a1d3c-0000-4000-8000-000000000000", "a.b"])
def test_remove_still_accepts_legitimate_ids(tmp_path: Path, job_id: str):
    paths = JobPaths(tmp_path)
    paths.create(job_id)
    paths.remove(job_id)
    assert not paths.job_dir(job_id).exists()


def test_input_file_sanitizes(tmp_path: Path):
    paths = JobPaths(tmp_path)
    resolved = paths.input_file("job-1", "../../evil.pdf")
    assert resolved == paths.input_dir("job-1") / "evil.pdf"


def test_image_file_rejects_traversal(tmp_path: Path):
    paths = JobPaths(tmp_path)
    with pytest.raises(ValueError):
        paths.image_file("job-1", "../../../etc/passwd")


def test_image_file_resolves_legitimate_name(tmp_path: Path):
    paths = JobPaths(tmp_path)
    resolved = paths.image_file("job-1", "picture-000.png")
    assert resolved == paths.images_dir("job-1") / "picture-000.png"


def test_table_file_naming(tmp_path: Path):
    paths = JobPaths(tmp_path)
    assert paths.table_file("job-1", 7, "csv").name == "table-007.csv"
    assert paths.table_file("job-1", 7, "html").name == "table-007.html"
