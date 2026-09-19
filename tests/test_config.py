from pathlib import Path

from app.config import Settings


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
