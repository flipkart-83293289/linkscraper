# syntax=docker/dockerfile:1

# Use Playwright's official image pinned to match the playwright==1.47.0
# pip package above. This bakes in Chromium + all its native OS deps
# (fonts, codecs, etc.) so we don't need `playwright install-deps` at
# runtime, which is the single biggest memory/build-time trap on Render's
# free tier build machines.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

# Install Python deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Chromium binaries are already present in the base image, but this is a
# harmless no-op safety net in case the base image tag drifts.
RUN python -m playwright install chromium --with-deps || true

COPY app ./app

# Render sets $PORT at runtime; default to 10000 for local testing.
ENV PORT=10000
ENV PYTHONUNBUFFERED=1

# Single uvicorn worker -- do NOT run multiple workers on a 512MB instance;
# each worker would spawn its own Chromium-capable process pool.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1"]
