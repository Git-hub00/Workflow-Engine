# Backend image — shared by the API, the Temporal worker, and the email adapter.
# All three run the same source (services/api imports services/worker) from one
# combined dependency set, mirroring the single shared virtualenv the systemd
# deployment used. Compose overrides `command:` per service.
#
# Build context = repo root:  docker build -f deploy/docker/backend.Dockerfile .
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install Python deps first for better layer caching.
COPY services/api/requirements.txt services/api/requirements.txt
RUN pip install -r services/api/requirements.txt

# Application source + migration/seed scripts + process definitions.
COPY services/api services/api
COPY services/worker services/worker
COPY scripts scripts
COPY definitions definitions

# The API resolves the `app` package relative to services/api. The worker and
# adapter are launched with absolute module paths, so this WORKDIR suits all
# commands (api / worker / adapter / migrate).
WORKDIR /app/services/api

EXPOSE 8000

# Default command runs the API. compose sets `command:` for worker/adapter/migrate.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
