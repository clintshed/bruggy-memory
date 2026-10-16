# bruggy-memory application image.
# Mirrors the bruggy-forever Docker convention; APP_VERSION is baked via
#   docker build --build-arg APP_VERSION=<sha>
FROM python:3.12-slim AS base

ARG APP_VERSION=0.0.0-dev
ENV APP_VERSION=${APP_VERSION} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install Python deps first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application source.
COPY src/ ./src/

# Runtime data (mounted as a named volume in compose).
RUN mkdir -p /app/data /app/staging /app/logs
VOLUME ["/app/data", "/app/staging", "/app/logs"]
ENV STAGING_DIR=/app/staging
ENV LOG_DIR=/app/logs

EXPOSE 8000

# Health check hits the API /health endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python - <<'PY' || exit 1
import sys, urllib.request, json
try:
    with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=4) as r:
        body = json.load(r)
    sys.exit(0 if body.get("status") == "ok" else 1)
except Exception:
    sys.exit(1)
PY

# Default command serves the API + scheduler. Override in compose with a
# subcommand (pull, catch-up, health) or args if needed.
CMD ["python", "-m", "uvicorn", "api_server:app", "--host", "0.0.0.0", "--port", "8000"]
