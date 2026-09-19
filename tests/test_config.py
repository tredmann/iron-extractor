from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings, TableMode


def test_api_key_list_splits_and_strips():
    settings = Settings(api_keys="alpha, beta ,,gamma", data_dir=Path("/tmp/x"))
    assert settings.api_key_list == ["alpha", "beta", "gamma"]


def test_default_ocr_language_list_splits():
    settings = Settings(api_keys="k", data_dir=Path("/tmp/x"), default_ocr_languages="en, de")
    assert settings.default_ocr_language_list == ["en", "de"]


def test_defaults_match_spec():
    settings = Settings(api_keys="k", data_dir=Path("/tmp/x"))
    assert settings.worker_threads == 2
    assert settings.queue_max == 32
    assert settings.job_timeout_seconds == 600.0
    assert settings.converter_cache_size == 4
    assert settings.default_ocr is True
    assert settings.default_table_mode == "accurate"
    assert settings.default_image_scale == 2.0
    assert settings.docs_enabled is True


def test_bad_table_mode_fails_at_startup():
    """A typo in DEFAULT_TABLE_MODE used to sail through startup and then
    make *every* upload return 400 "options.table_mode: Input should be
    'fast' or 'accurate'" -- blaming the caller for a server misconfig."""
    with pytest.raises(ValidationError):
        Settings(api_keys="k", data_dir=Path("/tmp/x"), default_table_mode="perfect")


def test_table_mode_default_is_the_enum():
    settings = Settings(api_keys="k", data_dir=Path("/tmp/x"))
    assert settings.default_table_mode is TableMode.ACCURATE
    assert settings.default_table_mode == "accurate"


@pytest.mark.parametrize(
    "field", ["worker_threads", "queue_max", "converter_cache_size", "max_upload_bytes"]
)
@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_sizes_are_rejected(field: str, value: int):
    """`max(1, ...)` at the point of use hid these. QUEUE_MAX=0 in
    particular became `queue.Queue(maxsize=0)` -- an *unbounded* queue,
    turning the 503 backpressure into an OOM."""
    with pytest.raises(ValidationError):
        Settings(api_keys="k", data_dir=Path("/tmp/x"), **{field: value})


def test_max_upload_bytes_default():
    assert Settings(api_keys="k", data_dir=Path("/tmp/x")).max_upload_bytes == 200 * 1024 * 1024
