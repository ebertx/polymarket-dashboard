# ===================================
# HARDENED DOCKERFILE FOR POLYMARKET DASHBOARD
# ===================================

# Build stage
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --user -r requirements.txt

# ===================================
# Production stage - HARDENED
# ===================================
FROM python:3.11-slim AS runner

# Install security updates and minimal runtime dependencies
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends \
    dumb-init \
    tini \
    && rm -rf /var/lib/apt/lists/* && \
    apt-get clean

WORKDIR /app

# Create non-root user with no shell
RUN groupadd -g 1001 appuser && \
    useradd -r -u 1001 -g appuser -s /sbin/nologin appuser

# Copy Python packages from builder
COPY --from=builder --chown=appuser:appuser /root/.local /home/appuser/.local

# Copy application code
COPY --chown=appuser:appuser . .

# Set restrictive permissions (chown first, then chmod so appuser can read)
RUN chown -R appuser:appuser /app && \
    chmod -R 550 /app && \
    mkdir -p /tmp/polymarket && \
    chown appuser:appuser /tmp/polymarket && \
    chmod 770 /tmp/polymarket

# Update PATH for user-installed packages
ENV PATH=/home/appuser/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Switch to non-root user
USER appuser

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=5)"

# Use tini as init system
ENTRYPOINT ["/usr/bin/tini", "--"]

# Run application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--limit-concurrency", "50"]
