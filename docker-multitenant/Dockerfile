# Multi-Tenant Synapse Demo Docker Image
FROM python:3.11-slim-bookworm

# Install minimal system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libffi-dev \
    libsodium-dev \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install minimal Python dependencies for demo
RUN pip install --no-cache-dir \
    "signedjson>=1.1.0" \
    "PyNaCl>=1.4.0" \
    "unpaddedbase64>=2.1.0"

# Copy scripts only (standalone server)
COPY docker-multitenant/scripts/ /app/scripts/

# Set environment
ENV PYTHONPATH=/app

# Create directories
RUN mkdir -p /data /media /keys && \
    chmod 777 /data /media /keys

# Health check
HEALTHCHECK --interval=10s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8008/health || exit 1

EXPOSE 8008 8448

# Default command - run the standalone multi-tenant demo server
CMD ["python3", "/app/scripts/run_server.py"]
