# Multi-stage Dockerfile for Edge HVAC Proxy
# Supports both CPU and GPU (NVIDIA) builds

# ═══════════════════════════════════════════════════════════════════════════════
# BASE STAGE
# ═══════════════════════════════════════════════════════════════════════════════

FROM python:3.11-slim AS base

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libpq-dev \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -r edgehvac && useradd -r -g edgehvac -d /app -s /bin/bash edgehvac

WORKDIR /app

# Copy dependency files
COPY pyproject.toml ./

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e . --no-build-isolation

# ═══════════════════════════════════════════════════════════════════════════════
# GPU STAGE (optional - for NVIDIA GPU support)
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
# TFLITE STAGE (optional - for TensorFlow Lite)
# ═══════════════════════════════════════════════════════════════════════════════

FROM base AS tflite

RUN pip install --no-cache-dir \
    tflite-runtime==2.16.1

# ═══════════════════════════════════════════════════════════════════════════════
# PRODUCTION STAGE
# ═══════════════════════════════════════════════════════════════════════════════

FROM python:3.11-slim AS production

# Runtime dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -r edgehvac && useradd -r -g edgehvac -d /app -s /bin/bash edgehvac

WORKDIR /app

# Copy installed packages from base
COPY --from=base /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=base /usr/local/bin /usr/local/bin

# Copy application code
COPY --chown=edgehvac:edgehvac . .

# Create data directories
RUN mkdir -p /app/data/vendors /app/models /app/certs /app/logs && \
    chown -R edgehvac:edgehvac /app

USER edgehvac

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Expose ports
EXPOSE 8911 9090 8080

# Entry point
ENTRYPOINT ["python", "-m", "core.server"]

# ═══════════════════════════════════════════════════════════════════════════════
# GPU PRODUCTION STAGE
# ═══════════════════════════════════════════════════════════════════════════════

FROM python:3.11-slim AS production-gpu

# NVIDIA runtime dependencies
ENV NVIDIA_VISIBLE_DEVICES=all
ENV NVIDIA_DRIVER_CAPABILITIES=compute,utility

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -r edgehvac && useradd -r -g edgehvac -d /app -s /bin/bash edgehvac

WORKDIR /app

COPY --from=gpu /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=gpu /usr/local/bin /usr/local/bin
COPY --from=gpu /usr/local/cuda /usr/local/cuda

ENV CUDA_HOME=/usr/local/cuda
ENV PATH=${CUDA_HOME}/bin:${PATH}
ENV LD_LIBRARY_PATH=${CUDA_HOME}/lib64:${LD_LIBRARY_PATH}

COPY --chown=edgehvac:edgehvac . .

RUN mkdir -p /app/data/vendors /app/models /app/certs /app/logs && \
    chown -R edgehvac:edgehvac /app

USER edgehvac

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

EXPOSE 8911 9090 8080

ENTRYPOINT ["python", "-m", "core.server"]