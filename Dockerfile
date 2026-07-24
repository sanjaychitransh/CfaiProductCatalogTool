# Single-stage build for IBM Code Engine deployment
FROM registry.access.redhat.com/ubi9/python-311:latest

# Switch to root for installations
USER 0

WORKDIR /app

# Install build dependencies (gcc needed for ahocorasick C extension)
# Clean up in same layer to reduce image size
RUN dnf install -y gcc && \
    dnf clean all && \
    rm -rf /var/cache/dnf

# Copy and install production-only Python dependencies
COPY config/requirements-prod.txt .
RUN pip install --no-cache-dir -r requirements-prod.txt && \
    dnf remove -y gcc && \
    dnf clean all && \
    rm -rf /var/cache/dnf /root/.cache

# Copy application code and data
COPY src/ ./src/
COPY data/ ./data/
COPY app.py .

# Set environment variables for Code Engine
ENV PYTHONPATH=/app \
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

EXPOSE 8080

CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
