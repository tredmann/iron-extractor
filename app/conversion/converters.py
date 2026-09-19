import logging
import threading
from collections import OrderedDict
from pathlib import Path

from app.api.schemas import ConversionOptions, TableMode

logger = logging.getLogger(__name__)


class ConverterProvider:
    """Supplies docling converters, cached per thread in a small LRU.

    Every docling import happens inside a method, so importing this module
    costs nothing and the fast test suite runs without torch installed. Each
    cached converter holds live model pipelines, so the cache is deliberately
    small.
    """

    def __init__(
        self,
        models_dir: Path | None,
        timeout_seconds: float | None,
        cache_size: int = 4,
    ) -> None:
        self._models_dir = models_dir
        self._timeout_seconds = timeout_seconds
        self._cache_size = max(1, cache_size)
        self._local = threading.local()

    def build_pipeline_options(self, options: ConversionOptions):
        from docling.datamodel.pipeline_options import (
            EasyOcrOptions,
            PdfPipelineOptions,
            TableFormerMode,
            TableStructureOptions,
        )

        mode = TableFormerMode.FAST if options.table_mode is TableMode.FAST else TableFormerMode.ACCURATE

        pipeline_options = PdfPipelineOptions(
            do_ocr=options.ocr,
            do_table_structure=True,
            table_structure_options=TableStructureOptions(do_cell_matching=True, mode=mode),
            # EasyOCR is pinned rather than docling's OcrAutoOptions so behavior
            # is deterministic and the offline container never probes for an
            # engine it does not ship.
            ocr_options=EasyOcrOptions(
                lang=list(options.ocr_languages),
                download_enabled=self._models_dir is None,
            ),
            images_scale=options.image_scale,
            generate_picture_images=True,
            generate_page_images=False,
            document_timeout=self._timeout_seconds,
        )
        if self._models_dir is not None:
            pipeline_options.artifacts_path = self._models_dir
        return pipeline_options

    def get(self, options: ConversionOptions):
        """Return a converter for these options, reusing one where possible."""
        cache = self._cache()
        key = options.cache_key()

        if key in cache:
            cache.move_to_end(key)
            return cache[key]

        converter = self._build_converter(options)
        cache[key] = converter
        while len(cache) > self._cache_size:
            evicted, _ = cache.popitem(last=False)
            logger.info("Evicted cached converter for options %s", evicted)
        return converter

    def cached_key_count(self) -> int:
        return len(self._cache())

    def _cache(self) -> OrderedDict:
        cache = getattr(self._local, "cache", None)
        if cache is None:
            cache = OrderedDict()
            self._local.cache = cache
        return cache

    def _build_converter(self, options: ConversionOptions):
        from docling.datamodel.base_models import InputFormat
        from docling.document_converter import DocumentConverter, PdfFormatOption

        logger.info("Building docling converter for options %s", options.cache_key())
        return DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(
                    pipeline_options=self.build_pipeline_options(options)
                )
            }
        )
