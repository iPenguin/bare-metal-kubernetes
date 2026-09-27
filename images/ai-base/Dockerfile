# Minimal base for AI apps that bootstrap their own python/torch env (Easy Diffusion, Forge).
# No driver or CUDA toolkit here: the nvidia runtime injects the host driver, and torch wheels
# bundle their own CUDA runtime. Blackwell (RTX 50xx) needs torch built for CUDA 12.8+.
ARG BASE_IMAGE=nvidia/cuda:12.8.1-base-ubuntu24.04
FROM ${BASE_IMAGE}

# curl/bzip2/unzip: installers fetch micromamba and zip releases
# libgl1/libglib2.0-0: loaded by opencv at runtime
RUN apt-get update \
  && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    bzip2 \
    ca-certificates \
    curl \
    git \
    libgl1 \
    libglib2.0-0 \
    unzip \
  && rm -rf /var/lib/apt/lists/*

ENV NVIDIA_DRIVER_CAPABILITIES=compute,utility
