# Multi-stage Dockerfile for Ride the API — Local Cloud Replacement Proxy
# Supports CPU-only (default) and GPU (NVIDIA) builds

# ═══════════════════════════════════════════════════════════════════════════════
# BASE STAGE
# ═══════════════════════════════════════════════════════════════════════════════

FROM python:3.14-slim AS base

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libpq-dev \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -r rideapi && useradd -r -g rideapi -d /app -s /bin/bash rideapi

WORKDIR /app

# Copy dependency files
COPY pyproject.toml ./

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e . --no-build-isolation

# ═══════════════════════════════════════════════════════════════════════════════
# GPU STAGE (optional — for NVIDIA GPU inference acceleration)
# ═══════════════════════════════════════════════════════════════════════════════

FROM base AS gpu

# Install NVIDIA CUDA toolkit (for TensorRT/ONNX Runtime GPU)
# Note: Requires nvidia-container-toolkit on host
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    gnupg2 \
    && wget https://developer.download.nvidia.com/compute/cuda/repos/debian12/x86_64/cuda-keyring_1.1-1_all.deb \
    && dpkg -i cuda-keyring_1.1-1_all.deb \
    && apt-get update && apt-get install -y --no-install-recommends \
    cuda-toolkit-12-4 \
    && rm -rf /var/lib/apt/lists/* cuda-keyring_1.1-1_all.deb

ENV CUDA_HOME=/usr/local/cuda
ENV PATH=${CUDA_HOME}/bin:${PATH}
ENV LD_LIBRARY_PATH=${CUDA_HOME}/lib64:${LD_LIBRARY_PATH}

# Install GPU-enabled packages
RUN pip install --no-cache-dir \
    onnxruntime-gpu==1.18.0 \
    tensorrt==10.0.0

# ═══════════════════════════════════════════════════════════════════════════════
# PRODUCTION STAGE
# ═══════════════════════════════════════════════════════════════════════════════

FROM python:3.14-slim AS production

# Runtime dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -r rideapi && useradd -r -g rideapi -d /app -s /bin/bash rideapi

WORKDIR /app

# Copy installed packages from base
COPY --from=base /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=base /usr/local/bin /usr/local/bin

# Copy application code
COPY --chown=rideapi:rideapi . .

# Create data directories
RUN mkdir -p /app/data/devices /app/data/core /app/models /app/certs /app/logs && \
    chown -R rideapi:rideapi /app

USER rideapi

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8911/health || exit 1

# Expose ports: 8911 proxy, 8080 health/metrics, 9090 optional metrics
EXPOSE 8911 8080 9090

# Entry point
ENTRYPOINT ["python", "-m", "core.server"]

# ═══════════════════════════════════════════════════════════════════════════════
# GPU PRODUCTION STAGE
# ═══════════════════════════════════════════════════════════════════════════════

FROM python:3.14-slim AS production-gpu

# NVIDIA runtime dependencies
ENV NVIDIA_VISIBLE_DEVICES=all
ENV NVIDIA_DRIVER_CAPABILITIES=compute,utility

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -r rideapi && useradd -r -g rideapi -d /app -s /bin/bash rideapi

WORKDIR /app

COPY --from=gpu /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=gpu /usr/local/bin /usr/local/bin
COPY --from=gpu /usr/local/cuda /usr/local/cuda

ENV CUDA_HOME=/usr/local/cuda
ENV PATH=${CUDA_HOME}/bin:${PATH}
ENV LD_LIBRARY_PATH=${CUDA_HOME}/lib64:${LD_LIBRARY_PATH}

COPY --chown=rideapi:rideapi . .

RUN mkdir -p /app/data/devices /app/data/core /app/models /app/certs /app/logs && \
    chown -R rideapi:rideapi /app

USER rideapi

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8911/health || exit 1

EXPOSE 8911 8080 9090

ENTRYPOINT ["python", "-m", "core.server"]