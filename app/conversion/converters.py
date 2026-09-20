import logging
import threading
from collections import OrderedDict
from pathlib import Path

from app.api.schemas import ConversionOptions, TableMode

logger = logging.getLogger(__name__)

# docling stores EasyOCR weights under this subdirectory of artifacts_path
# (EasyOcrModel._model_repo_folder). Mirrored rather than imported so this
# module keeps its no-docling-at-import-time property.
_EASYOCR_FOLDER = "EasyOcr"


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
        if cache_size < 1:
            raise ValueError(f"cache_size must be at least 1, got {cache_size}")
        self._models_dir = models_dir
        self._timeout_seconds = timeout_seconds
        self._cache_size = cache_size
        self._local = threading.local()
        # Flipped the first time a converter is actually built, which is
        # the first time docling reads any weight off disk. /health reports
        # this rather than a constant, so the container healthcheck cannot
        # go green while /opt/models is unreadable. An Event because worker
        # threads set it and the event loop reads it.
        self._ready = threading.Event()

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

    def unsupported_ocr_reason(self, options: ConversionOptions) -> str | None:
        """Why this server cannot OCR this request, or None if it can.

        Answered from the weights actually on disk. When `models_dir` is
        set, docling forces EasyOCR's `download_enabled` to False (see
        EasyOcrModel.__init__), so a recognizer that was not baked into the
        image cannot be fetched at conversion time -- EasyOCR raises a bare
        `FileNotFoundError` from inside a worker thread, roughly a second
        after the caller already got their 202. Asking here turns that into
        a 400 on the request that caused it.

        Returns a message rather than raising, so the API layer owns the
        status code and this layer stays free of HTTP concepts.
        """
        # `do_ocr=False` means EasyOcrModel never constructs a Reader and
        # never reads a weight, so the language list is inert -- do not
        # reject a request that would have worked.
        if not options.ocr or self._models_dir is None:
            return None

        from easyocr.config import all_lang_list, detection_models, recognition_models

        requested = list(options.ocr_languages)
        unknown = sorted(set(requested) - set(all_lang_list))
        if unknown:
            return f"EasyOCR does not recognize the language code(s) {unknown}."

        generation, key, compatible = self._resolve_recognizer(requested)
        if compatible is not None:
            extra = sorted(set(requested) - set(compatible))
            if extra:
                return (
                    f"EasyOCR cannot combine {extra} with the script selected by "
                    f"{sorted(set(requested) & set(compatible))}; that model is only "
                    f"compatible with English."
                )

        folder = self._models_dir / _EASYOCR_FOLDER
        needed = [
            detection_models["craft"]["filename"],
            recognition_models[generation][key]["filename"],
        ]
        missing = [name for name in needed if not (folder / name).is_file()]
        if missing:
            # Filenames only, never the resolved path: this string is
            # returned to an HTTP caller.
            return (
                f"This server has no OCR weights for {requested}. It would need "
                f"{missing}, which this image was not built with. Rebuild the image "
                f"with a matching --easyocr-lang flag to enable them."
            )
        return None

    def _resolve_recognizer(self, requested: list[str]) -> tuple[str, str, list[str] | None]:
        """Pick the EasyOCR recognizer for a language set, as EasyOCR would.

        A faithful port of the auto-detect ladder in `easyocr.Reader.__init__`,
        in the same order -- including the exact list comparison `== ["en"]`,
        which is why ["en"] gets the small English-only model while
        ["de", "en"] falls all the way through to Latin. Only the dispatch is
        ported; every language list and filename is read back out of
        `easyocr.config`, so an EasyOCR upgrade cannot leave stale data here.

        Returns (generation, model key, languages the model accepts). The
        third element is None for the Latin model, which imposes no
        compatibility restriction of its own.
        """
        from easyocr.config import (
            arabic_lang_list,
            bengali_lang_list,
            cyrillic_lang_list,
            devanagari_lang_list,
        )

        if requested == ["en"]:
            return "gen2", "english_g2", ["en"]

        # Single-language markers, tested in EasyOCR's order. Each of these
        # models accepts only its own language plus English.
        for marker, generation, key in (
            ("th", "gen1", "thai_g1"),
            ("ch_tra", "gen1", "zh_tra_g1"),
            ("ch_sim", "gen2", "zh_sim_g2"),
            ("ja", "gen2", "japanese_g2"),
            ("ko", "gen2", "korean_g2"),
            ("ta", "gen1", "tamil_g1"),
            ("te", "gen2", "telugu_g2"),
            ("kn", "gen2", "kannada_g2"),
        ):
            if marker in requested:
                return generation, key, [marker, "en"]

        for group, generation, key in (
            (bengali_lang_list, "gen1", "bengali_g1"),
            (arabic_lang_list, "gen1", "arabic_g1"),
            (devanagari_lang_list, "gen1", "devanagari_g1"),
            (cyrillic_lang_list, "gen2", "cyrillic_g2"),
        ):
            if set(requested) & set(group):
                return generation, key, list(group) + ["en"]

        return "gen2", "latin_g2", None

    def get(self, options: ConversionOptions):
        """Return a converter for these options, reusing one where possible."""
        cache = self._cache()
        key = options.cache_key()

        if key in cache:
            cache.move_to_end(key)
            return cache[key]

        converter = self._build_converter(options)
        self._ready.set()
        cache[key] = converter
        while len(cache) > self._cache_size:
            evicted, _ = cache.popitem(last=False)
            logger.info("Evicted cached converter for options %s", evicted)
        return converter

    def models_loaded(self) -> bool:
        """True once at least one converter has been built successfully.

        Process-wide, not per-thread: the question /health answers is
        whether the model weights are readable at all, not whether the
        thread serving this request happens to hold a cached converter.
        """
        return self._ready.is_set()

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
