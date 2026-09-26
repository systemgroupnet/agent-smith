# Base image and package index are build arguments so a build behind a mirror
# (or an air-gapped registry) needs no edit here:
#   docker build --build-arg BASE_IMAGE=<registry>/python:3.13-slim \
#                --build-arg PIP_INDEX_URL=<mirror>/simple .
ARG BASE_IMAGE=python:3.13-slim
FROM ${BASE_IMAGE}

ARG PIP_INDEX_URL=https://pypi.org/simple

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install uv -i "$PIP_INDEX_URL"

COPY requirements.txt .

RUN uv pip install -i "$PIP_INDEX_URL" --system -r requirements.txt

COPY . .

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Low-traffic service (one event per review run); a single worker with threads
# is ample. Alembic migrations run at import (see main.py) before serving.
CMD ["gunicorn", "-b", "0.0.0.0:8000", "-w", "1", "--threads", "4", "main:app"]
