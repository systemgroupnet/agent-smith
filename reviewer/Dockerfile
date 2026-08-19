FROM registry.example.com/python:3.13-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# git: the reviewer clones the MR's source branch so agents read and grep a real
# checkout instead of naming a ref against a remote API (see
# core/utils/repo_clone.py). ripgrep: what the filesystem grep tool shells out to;
# without it the tool falls back to a slow pure-regex walk over a monorepo.
# python:*-slim ships neither.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ripgrep ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install uv -i https://pypi.org/simple

COPY requirements.txt .

RUN uv pip install -i https://pypi.org/simple --system -r requirements.txt

COPY . .

RUN chmod +x main.py

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER root

CMD ["python", "main.py", "--help"]
