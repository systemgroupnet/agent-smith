FROM registry.example.com/python:3.13-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    COLLECTOR_DB_PATH=/data/collector.db

RUN pip install uv -i https://pypi.org/simple

COPY requirements.txt .

RUN uv pip install -i https://pypi.org/simple --system -r requirements.txt

COPY . .

# Create the app user and the /data mount point up front, owned by that user, so a
# named volume mounted at /data inherits uid 1000 ownership on first mount and the
# non-root process can write the SQLite file.
RUN useradd -m -u 1000 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data
USER appuser

EXPOSE 8000

# Single worker keeps SQLite writes serialized; threads handle concurrent reads.
CMD ["gunicorn", "-b", "0.0.0.0:8000", "-w", "1", "--threads", "4", "main:app"]
