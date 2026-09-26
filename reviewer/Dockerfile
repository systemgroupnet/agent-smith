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

# git: the reviewer clones the MR's source branch so agents read and grep a real
# checkout instead of naming a ref against a remote API (see
# core/utils/repo_clone.py). ripgrep: what the filesystem grep tool shells out to;
# without it the tool falls back to a slow pure-regex walk over a monorepo.
# python:*-slim ships neither.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ripgrep ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install uv -i "$PIP_INDEX_URL"

COPY requirements.txt .

RUN uv pip install -i "$PIP_INDEX_URL" --system -r requirements.txt

COPY . .

# Which review profile (profiles/<name>.yml, or a path) the image uses unless the
# job sets REVIEW_PROFILE itself. See profiles/default.yml.
ARG DEFAULT_REVIEW_PROFILE=default
ENV REVIEW_PROFILE=${DEFAULT_REVIEW_PROFILE}

RUN chmod +x main.py

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER root

CMD ["python", "main.py", "--help"]
