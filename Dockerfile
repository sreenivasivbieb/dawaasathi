# Dawaa Saathi: one container, plain HTTP on $PORT. HTTPS is terminated by the host
# (Render / Fly.io / Railway). Keys come from the host's environment, never from the image.
#
#   docker build -t dawasaathi .                                   # DB exclusively from data1
#   docker run -p 8000:8000 -e GNANI_API_KEY=... -e GROQ_API_KEY=... dawasaathi

FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

ARG DATA_DIR=data1
# Optional Kaggle "A-Z Medicine Dataset of India" CSV (path inside the build context).
ARG AZ_CSV=
ARG AZ_AS_OF=
ARG AZ_LIMIT=20000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH" \
    PORT=8000 \
    FORWARDED_ALLOW_IPS="*" \
    DAWA_DB_PATH=/app/var/dawasaathi.db \
    DAWA_CACHE_DIR=/app/var/tts_cache

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tesseract-ocr fonts-dejavu-core poppler-utils \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first (cached layer), then the code.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

# Build the SQLite DB from data1 and the synthetic test images at image build time.
RUN if [ -n "$AZ_CSV" ]; then \
      python -m scripts.load_data --src "$DATA_DIR" --az "$AZ_CSV" --az-as-of "$AZ_AS_OF" --az-limit "$AZ_LIMIT"; \
    else \
      python -m scripts.load_data --src "$DATA_DIR"; \
    fi \
 && python -m scripts.make_samples

# Non-root runtime; /app/var holds the DB (read) and the TTS segment cache (written).
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin app \
 && mkdir -p /app/var/tts_cache \
 && chown -R app:app /app/var
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8000'), timeout=4)" || exit 1

# One worker: sessions and reply-audio jobs live in process memory.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers"]
