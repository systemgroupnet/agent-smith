FROM python:3.11-slim
 
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install uv

COPY requirements.txt .

RUN uv pip install -i https://pypi.org/simple --system -r requirements.txt

COPY . .

RUN chmod +x main.py

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER root

CMD ["python", "main.py", "--help"]
