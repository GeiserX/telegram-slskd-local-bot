FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies (libsndfile1 needed for FLAC spectral analysis)
RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends libsndfile1 && \
    rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ ./src/
COPY scripts/ ./scripts/

# Create non-root user for security
RUN useradd -m -u 1000 slskdimporter && \
    mkdir -p /downloads /music && \
    chown -R slskdimporter:slskdimporter /app /downloads /music && \
    chmod +x /app/scripts/entrypoint.sh

# Switch to non-root user
USER slskdimporter

# Set default environment variables
ENV LOG_LEVEL=INFO \
    PYTHONPATH=/app/src \
    HEALTH_PORT=8080

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

# Volumes for downloads and output music
VOLUME ["/downloads", "/music"]

# Expose health check port
EXPOSE 8080

ENTRYPOINT ["/app/scripts/entrypoint.sh"]

# Default: run the bot
CMD ["python", "-m", "music_downloader", "run"]
