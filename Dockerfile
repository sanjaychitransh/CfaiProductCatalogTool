# Multi-stage build for IBM Code Engine deployment
# Stage 1: Builder
FROM registry.access.redhat.com/ubi9/python-311:latest AS builder

# Switch to root for installations
USER 0

WORKDIR /app

# Install build dependencies
RUN dnf install -y gcc && dnf clean all

# Copy and install Python dependencies
COPY config/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Stage 2: Runtime
FROM registry.access.redhat.com/ubi9/python-311:latest

# Switch to root for setup
USER 0

WORKDIR /app

# Copy Python packages from builder
COPY --from=builder /usr/local /usr/local

# Copy application code
COPY src/ ./src/
COPY data/ ./data/
COPY app.py .

# Set environment variables for Code Engine
ENV PATH=/usr/local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    USE_ENHANCED_MATCHER=true \
    PORT=8080

# Set permissions for OpenShift/Code Engine (arbitrary user IDs)
RUN chown -R 1001:0 /app && \
    chmod -R g=u /app && \
    chmod -R 775 /app

# Switch to non-root user
USER 1001

# Expose Code Engine default port
EXPOSE 8080

# Health check for Code Engine
HEALTHCHECK --interval=30s --timeout=3s --start-period=40s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:8080/health')" || exit 1

# Run with uvicorn for production
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]