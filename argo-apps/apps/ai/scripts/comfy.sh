#!/bin/bash
# Install (on first run or version change) and launch ComfyUI on the shared ai-applications volume.
# The ai-base image has no python, so uv bootstraps a standalone interpreter and venv on the
# volume. Bump COMFY_VERSION to upgrade; models/outputs/custom_nodes in the checkout are kept.
set -euo pipefail

APP_DIR="${APP_DIR:-/srv/ai/comfy}"
COMFY_VERSION="${COMFY_VERSION:-v0.38.0}"
COMFY_REPO="${COMFY_REPO:-https://github.com/comfyanonymous/ComfyUI.git}"
PYTHON_VERSION="${PYTHON_VERSION:-3.13}"
# cu130 needs driver >= 580 and is what upstream requires for RTX 20 series and newer
TORCH_BACKEND="${TORCH_BACKEND:-cu130}"
COMFY_PORT="${COMFY_PORT:-8188}"

SRC_DIR="$APP_DIR/ComfyUI"
VENV_DIR="$APP_DIR/venv-py$PYTHON_VERSION"
STAMP="$VENV_DIR/.comfy-version"

export UV_INSTALL_DIR="$APP_DIR/bin"
export UV_PYTHON_INSTALL_DIR="$APP_DIR/python"
export UV_CACHE_DIR="$APP_DIR/.cache/uv"
# cache and venv live on cephfs; don't rely on hardlinks
export UV_LINK_MODE=copy
export PATH="$UV_INSTALL_DIR:$PATH"

mkdir -p "$APP_DIR"
cd "$APP_DIR"

if ! command -v uv >/dev/null; then
  echo "Installing uv into $UV_INSTALL_DIR"
  curl -fsSL --retry 3 https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
fi

# Clone on first run, otherwise move the existing checkout to the pinned tag
if [ ! -d "$SRC_DIR/.git" ]; then
  echo "Cloning ComfyUI $COMFY_VERSION into $SRC_DIR"
  git clone --branch "$COMFY_VERSION" "$COMFY_REPO" "$SRC_DIR"
elif [ "$(git -C "$SRC_DIR" describe --tags --exact-match 2>/dev/null || true)" != "$COMFY_VERSION" ]; then
  echo "Updating ComfyUI checkout to $COMFY_VERSION"
  git -C "$SRC_DIR" fetch --tags --force origin
  git -C "$SRC_DIR" checkout --force "$COMFY_VERSION"
fi

if [ ! -x "$VENV_DIR/bin/python" ]; then
  uv venv --python "$PYTHON_VERSION" "$VENV_DIR"
fi

# (Re)install dependencies only when the pinned version changes
if [ "$(cat "$STAMP" 2>/dev/null || true)" != "$COMFY_VERSION" ]; then
  echo "Installing ComfyUI $COMFY_VERSION dependencies"
  export VIRTUAL_ENV="$VENV_DIR"
  uv pip install --torch-backend "$TORCH_BACKEND" torch torchvision torchaudio \
    -r "$SRC_DIR/requirements.txt" -r "$SRC_DIR/manager_requirements.txt"
  echo "$COMFY_VERSION" > "$STAMP"
fi

"$VENV_DIR/bin/python" -c 'import torch; print("torch", torch.__version__, "cuda:", torch.cuda.is_available())'

cd "$SRC_DIR"
exec "$VENV_DIR/bin/python" main.py --listen 0.0.0.0 --port "$COMFY_PORT" --enable-manager
