# Use Python 3.11 slim image as base
FROM python:3.11-slim
 
# Set working directory
WORKDIR /app

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install system dependencies
# Docker CLI is needed for MCP Atlassian integration via docker exec
# RUN apt-get update && apt-get install -y --no-install-recommends \
#     git \
#     ca-certificates \
#     curl \
#     && install -m 0755 -d /etc/apt/keyrings \
#     && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
#     && chmod a+r /etc/apt/keyrings/docker.asc \
#     && echo \
#     "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian \
#     $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
#     tee /etc/apt/sources.list.d/docker.list > /dev/null \
#     && apt-get update \
#     && apt-get install -y --no-install-recommends docker-ce-cli \
#     && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better layer caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Make main.py executable
RUN chmod +x main.py

# Create a non-root user for security
# Note: User needs access to Docker socket for MCP integration
# If running in Docker with socket mount, ensure proper permissions on host
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
# Run as root to access Docker socket (required for docker exec)
# For production, consider using docker group permissions instead
USER root

# Default command (can be overridden)
CMD ["python", "main.py", "--help"]
