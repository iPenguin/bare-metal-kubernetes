# Minimal base for AI apps that bootstrap their own python/torch env (Easy Diffusion, Forge).
# No driver or CUDA toolkit here: the nvidia runtime injects the host driver, and torch wheels
# bundle their own CUDA runtime. Blackwell (RTX 50xx) needs torch built for CUDA 12.8+.
ARG BASE_IMAGE=docker.io/nvidia/cuda:12.8.1-base-ubuntu24.04
FROM ${BASE_IMAGE}

# build-essential/python3-dev/cuda-cudart-dev: triton JIT-compiles a small C helper (driver.c)
#           against Python.h and cuda.h on first use, e.g. torch 2.14 routes RoPE matmuls in
#           text encoders through triton kernels
# curl/bzip2/unzip: installers fetch micromamba and zip releases
# libgl1/libglib2.0-0: loaded by opencv at runtime
# pciutils: torchruntime (Easy Diffusion) detects GPUs via `lspci -nn`, and silently
#           falls back to CPU-only torch if lspci is missing
RUN CUDA_PKG_VERSION="$(echo "${CUDA_VERSION%.*}" | tr . -)" \
  && apt-get update \
  && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    build-essential \
    bzip2 \
    ca-certificates \
    "cuda-cudart-dev-${CUDA_PKG_VERSION}" \
    curl \
    git \
    libgl1 \
    libglib2.0-0 \
    pciutils \
    python3-dev \
    unzip \
  && rm -rf /var/lib/apt/lists/*

ENV NVIDIA_DRIVER_CAPABILITIES=compute,utility
