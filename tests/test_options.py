import json
from pathlib import Path

import pytest

from app.api.errors import ApiError
from app.api.schemas import ConversionOptions, TableMode, parse_options
from app.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(api_keys="k", data_dir=tmp_path)


def test_defaults_come_from_settings(settings: Settings):
    options = parse_options(None, settings)
    assert options.ocr is True
    assert options.ocr_languages == ["en"]
    assert options.table_mode is TableMode.ACCURATE
    assert options.image_scale == 2.0


def test_explicit_values_override_defaults(settings: Settings):
    options = parse_options(
        json.dumps({"ocr": False, "ocr_languages": ["de"], "table_mode": "fast"}),
        settings,
    )
    assert options.ocr is False
    assert options.ocr_languages == ["de"]
    assert options.table_mode is TableMode.FAST


def test_blank_string_is_treated_as_absent(settings: Settings):
    assert parse_options("   ", settings).image_scale == 2.0


@pytest.mark.parametrize(
    ("given", "expected"),
    [(0.1, 1.0), (1.0, 1.0), (1.2, 1.0), (1.3, 1.5), (2.4, 2.5), (9.0, 4.0)],
)
def test_image_scale_is_clamped_and_quantized(settings: Settings, given, expected):
    options = parse_options(json.dumps({"image_scale": given}), settings)
    assert options.image_scale == expected


def test_unknown_field_is_rejected(settings: Settings):
    with pytest.raises(ApiError) as excinfo:
        parse_options(json.dumps({"colour": "blue"}), settings)
    assert excinfo.value.status_code == 400


def test_bad_table_mode_is_rejected(settings: Settings):
    with pytest.raises(ApiError):
        parse_options(json.dumps({"table_mode": "perfect"}), settings)


def test_malformed_json_is_rejected(settings: Settings):
    with pytest.raises(ApiError) as excinfo:
        parse_options("{not json", settings)
    assert "JSON" in excinfo.value.message


def test_non_object_json_is_rejected(settings: Settings):
    with pytest.raises(ApiError):
        parse_options("[1, 2]", settings)


def test_empty_language_list_is_rejected(settings: Settings):
    with pytest.raises(ApiError):
        parse_options(json.dumps({"ocr_languages": []}), settings)


def test_blank_language_is_rejected(settings: Settings):
    with pytest.raises(ApiError):
        parse_options(json.dumps({"ocr_languages": ["  "]}), settings)


def test_cache_key_distinguishes_option_sets():
    first = ConversionOptions(ocr=True, ocr_languages=["en"], table_mode=TableMode.FAST, image_scale=2.0)
    same = ConversionOptions(ocr=True, ocr_languages=["en"], table_mode=TableMode.FAST, image_scale=2.0)
    other = ConversionOptions(ocr=True, ocr_languages=["de"], table_mode=TableMode.FAST, image_scale=2.0)
    assert first.cache_key() == same.cache_key()
    assert first.cache_key() != other.cache_key()
