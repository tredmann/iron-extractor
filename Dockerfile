# syntax=docker/dockerfile:1

FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# libgl1 transitively pulls in libxcb1 and friends. cv2 -- imported by the
# easyocr package that EasyOcrOptions requires (see converters.py) -- needs
# those at import time, and `docling-tools models download` below imports
# it just to resolve OCR language codes, even before any conversion runs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# CPU-only torch wheels: several GB smaller than the CUDA default.
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision

COPY requirements-base.txt requirements.txt ./
RUN pip install -r requirements.txt

# Bake the model weights in so the container starts offline and cold-starts fast.
#
# Named explicitly rather than the CLI's default set: the default set
# includes rapidocr (which imports cv2, which needs libxcb -- not present in
# this slim builder stage, so it crashes here) and omits easyocr entirely --
# yet EasyOcrOptions is the only OCR backend app/conversion/converters.py
# ever configures (see build_pipeline_options). "en" matches
# Settings.default_ocr_languages; requesting other OCR languages against
# this offline image requires rebuilding with more --easyocr-lang flags.
RUN docling-tools models download -o /opt/models layout tableformer easyocr --easyocr-lang en

# Pinned to match requirements-dev.txt. Not ruff -- that's a dev-only lint
# tool, irrelevant at runtime -- but pytest+httpx are what let this image
# run its own test suite (see Step 6/7 of this task) as the one place any
# of it ever executes against the real docling package.
RUN pip install pytest==9.1.0 httpx==0.28.1


FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    DATA_DIR=/data \
    MODELS_DIR=/opt/models

# libgomp1 is required by torch; the rest are common image/PDF runtime deps.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 libglib2.0-0 libgl1 curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/models /opt/models

RUN useradd --create-home --uid 10001 docling \
    && mkdir -p /data \
    && chown -R docling:docling /data

WORKDIR /srv
COPY --chown=docling:docling app ./app
COPY --chown=docling:docling tests ./tests
COPY --chown=docling:docling pytest.ini ./

USER docling
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# One worker only: the queue and thread pool live in this process.
CMD ["uvicorn", "app.main:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
