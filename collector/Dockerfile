FROM registry.example.com/python:3.13-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install uv -i https://pypi.org/simple

COPY requirements.txt .

RUN uv pip install -i https://pypi.org/simple --system -r requirements.txt

COPY . .

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Low-traffic service (one event per review run); a single worker with threads
# is ample. Alembic migrations run at import (see main.py) before serving.
CMD ["gunicorn", "-b", "0.0.0.0:8000", "-w", "1", "--threads", "4", "main:app"]
