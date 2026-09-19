import threading
from pathlib import Path

import pytest

from app.api.schemas import ConversionOptions, TableMode
from app.conversion.converters import ConverterProvider


@pytest.fixture
def provider() -> ConverterProvider:
    return ConverterProvider(models_dir=None, timeout_seconds=90.0, cache_size=2)


@pytest.mark.slow
def test_pipeline_options_map_ocr_off(provider):
    pytest.importorskip("docling", reason="real docling not installed")
    options = ConversionOptions(ocr=False, ocr_languages=["en"],
                                table_mode=TableMode.FAST, image_scale=1.0)
    pipeline = provider.build_pipeline_options(options)
    assert pipeline.do_ocr is False
    assert pipeline.do_table_structure is True
    assert pipeline.table_structure_options.mode.value == "fast"
    assert pipeline.images_scale == 1.0
    assert pipeline.generate_picture_images is True
    assert pipeline.generate_page_images is False
    assert pipeline.document_timeout == 90.0


@pytest.mark.slow
def test_pipeline_options_map_ocr_on(provider):
    pytest.importorskip("docling", reason="real docling not installed")
    options = ConversionOptions(ocr=True, ocr_languages=["de", "en"],
                                table_mode=TableMode.ACCURATE, image_scale=2.0)
    pipeline = provider.build_pipeline_options(options)
    assert pipeline.do_ocr is True
    assert pipeline.ocr_options.lang == ["de", "en"]
    assert pipeline.table_structure_options.mode.value == "accurate"


@pytest.mark.slow
def test_models_dir_becomes_artifacts_path(tmp_path: Path):
    pytest.importorskip("docling", reason="real docling not installed")
    provider = ConverterProvider(models_dir=tmp_path, timeout_seconds=None)
    pipeline = provider.build_pipeline_options(ConversionOptions())
    assert Path(pipeline.artifacts_path) == tmp_path
    assert pipeline.document_timeout is None


def test_same_options_reuse_one_converter(provider, monkeypatch):
    monkeypatch.setattr(
        ConverterProvider, "_build_converter", lambda self, options: object()
    )
    options = ConversionOptions()
    assert provider.get(options) is provider.get(options)
    assert provider.cached_key_count() == 1


def test_different_options_get_different_converters(provider, monkeypatch):
    monkeypatch.setattr(
        ConverterProvider, "_build_converter", lambda self, options: object()
    )
    first = provider.get(ConversionOptions(table_mode=TableMode.FAST))
    second = provider.get(ConversionOptions(table_mode=TableMode.ACCURATE))
    assert first is not second
    assert provider.cached_key_count() == 2


def test_cache_evicts_beyond_its_size(provider, monkeypatch):
    monkeypatch.setattr(
        ConverterProvider, "_build_converter", lambda self, options: object()
    )
    provider.get(ConversionOptions(image_scale=1.0))
    provider.get(ConversionOptions(image_scale=1.5))
    provider.get(ConversionOptions(image_scale=2.0))
    assert provider.cached_key_count() == 2


def test_cache_is_per_thread(provider, monkeypatch):
    monkeypatch.setattr(
        ConverterProvider, "_build_converter", lambda self, options: object()
    )
    provider.get(ConversionOptions())
    counts: list[int] = []

    def in_other_thread() -> None:
        counts.append(provider.cached_key_count())

    thread = threading.Thread(target=in_other_thread)
    thread.start()
    thread.join()

    assert counts == [0]


def test_models_loaded_is_false_until_a_converter_is_built(provider, monkeypatch):
    monkeypatch.setattr(
        ConverterProvider, "_build_converter", lambda self, options: object()
    )
    assert provider.models_loaded() is False
    provider.get(ConversionOptions())
    assert provider.models_loaded() is True


def test_models_loaded_is_process_wide_not_per_thread(provider, monkeypatch):
    """The question /health answers is whether weights are readable at all,
    not whether the thread serving this request holds a cached converter."""
    monkeypatch.setattr(
        ConverterProvider, "_build_converter", lambda self, options: object()
    )
    provider.get(ConversionOptions())

    seen: list[bool] = []
    thread = threading.Thread(target=lambda: seen.append(provider.models_loaded()))
    thread.start()
    thread.join()

    assert seen == [True]


def test_a_failed_build_leaves_models_unloaded(provider, monkeypatch):
    def boom(self, options):
        raise RuntimeError("/opt/models is unreadable")

    monkeypatch.setattr(ConverterProvider, "_build_converter", boom)

    with pytest.raises(RuntimeError):
        provider.get(ConversionOptions())
    assert provider.models_loaded() is False


def test_non_positive_cache_size_is_rejected():
    with pytest.raises(ValueError):
        ConverterProvider(models_dir=None, timeout_seconds=None, cache_size=0)
