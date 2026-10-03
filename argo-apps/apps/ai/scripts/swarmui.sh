#!/bin/bash
# Install (on first run or version change) and launch SwarmUI on the shared ai-applications volume.
# The ai-base image has no .NET or python, so dotnet-install puts the .NET SDK on the volume and
# uv bootstraps a standalone interpreter for SwarmUI's self-started ComfyUI backend. Bump
# SWARM_VERSION / COMFY_VERSION to upgrade; launch-linux.sh rebuilds SwarmUI when its checkout moves.
# Models live in MODELS_DIR (shared with the other AI apps); generated images live in DATA_DIR.
set -euo pipefail

APP_DIR="${APP_DIR:-/srv/ai/swarmui}"
SWARM_VERSION="${SWARM_VERSION:-0.9.8-Beta}"
SWARM_REPO="${SWARM_REPO:-https://github.com/mcmonkeyprojects/SwarmUI.git}"
DOTNET_CHANNEL="${DOTNET_CHANNEL:-8.0}"
COMFY_VERSION="${COMFY_VERSION:-v0.38.0}"
COMFY_REPO="${COMFY_REPO:-https://github.com/comfyanonymous/ComfyUI.git}"
PYTHON_VERSION="${PYTHON_VERSION:-3.13}"
# cu130 needs driver >= 580 and is what upstream requires for RTX 20 series and newer
TORCH_BACKEND="${TORCH_BACKEND:-cu130}"
# not SWARMUI_PORT: kubernetes sets that to tcp://<ip>:7801 for the swarmui service
LISTEN_PORT="${LISTEN_PORT:-7801}"
MODELS_DIR="${MODELS_DIR:-/srv/models}"
DATA_DIR="${DATA_DIR:-/srv/data/swarmui}"

SRC_DIR="$APP_DIR/SwarmUI"
COMFY_DIR="$APP_DIR/ComfyUI"
# SwarmUI only looks for the backend's python at <ComfyUI>/venv/bin/python3
VENV_DIR="$COMFY_DIR/venv"
STAMP="$VENV_DIR/.comfy-version"
# shared model folders SwarmUI doesn't already forward to ComfyUI under their ComfyUI names
MODEL_TYPES=(checkpoints vae loras embeddings controlnet upscale_models clip_vision text_encoders
  diffusion_models hypernetworks)

export DOTNET_ROOT="$APP_DIR/dotnet"
export DOTNET_CLI_TELEMETRY_OPTOUT=1
export DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1
export NUGET_PACKAGES="$APP_DIR/.cache/nuget"
export UV_INSTALL_DIR="$APP_DIR/bin"
export UV_PYTHON_INSTALL_DIR="$APP_DIR/python"
export UV_CACHE_DIR="$APP_DIR/.cache/uv"
# cache and venv live on cephfs; don't rely on hardlinks
export UV_LINK_MODE=copy
export PATH="$DOTNET_ROOT:$UV_INSTALL_DIR:$PATH"

mkdir -p "$APP_DIR"
cd "$APP_DIR"

if [ ! -x "$DOTNET_ROOT/dotnet" ] || ! dotnet --list-sdks | grep -q "^$DOTNET_CHANNEL\."; then
  echo "Installing .NET SDK $DOTNET_CHANNEL into $DOTNET_ROOT"
  curl -fsSL --retry 3 https://dot.net/v1/dotnet-install.sh \
    | bash -s -- --channel "$DOTNET_CHANNEL" --install-dir "$DOTNET_ROOT" --no-path
fi

if ! command -v uv >/dev/null; then
  echo "Installing uv into $UV_INSTALL_DIR"
  curl -fsSL --retry 3 https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
fi

# Clone on first run, otherwise move the existing checkout to the pinned tag. launch-linux.sh
# notices the new HEAD and rebuilds.
if [ ! -d "$SRC_DIR/.git" ]; then
  echo "Cloning SwarmUI $SWARM_VERSION into $SRC_DIR"
  git clone --branch "$SWARM_VERSION" "$SWARM_REPO" "$SRC_DIR"
elif [ "$(git -C "$SRC_DIR" describe --tags --exact-match 2>/dev/null || true)" != "$SWARM_VERSION" ]; then
  echo "Updating SwarmUI checkout to $SWARM_VERSION"
  git -C "$SRC_DIR" fetch --tags --force origin
  git -C "$SRC_DIR" checkout --force "$SWARM_VERSION"
fi

# Same for the ComfyUI backend. SwarmUI's own auto-update is turned off in the seeded backend
# settings below so it stays on this tag.
if [ ! -d "$COMFY_DIR/.git" ]; then
  echo "Cloning ComfyUI $COMFY_VERSION into $COMFY_DIR"
  git clone --branch "$COMFY_VERSION" "$COMFY_REPO" "$COMFY_DIR"
elif [ "$(git -C "$COMFY_DIR" describe --tags --exact-match 2>/dev/null || true)" != "$COMFY_VERSION" ]; then
  echo "Updating ComfyUI checkout to $COMFY_VERSION"
  git -C "$COMFY_DIR" fetch --tags --force origin
  git -C "$COMFY_DIR" checkout --force "$COMFY_VERSION"
fi

# --seed adds pip: SwarmUI pip-installs missing requirements for its own comfy nodes
if [ ! -x "$VENV_DIR/bin/python3" ]; then
  uv venv --seed --python "$PYTHON_VERSION" "$VENV_DIR"
fi

# (Re)install dependencies only when the pinned version changes
if [ "$(cat "$STAMP" 2>/dev/null || true)" != "$COMFY_VERSION" ]; then
  echo "Installing ComfyUI $COMFY_VERSION dependencies"
  export VIRTUAL_ENV="$VENV_DIR"
  uv pip install --torch-backend "$TORCH_BACKEND" torch torchvision torchaudio \
    -r "$COMFY_DIR/requirements.txt"
  echo "$COMFY_VERSION" > "$STAMP"
fi

"$VENV_DIR/bin/python3" -c 'import torch; print("torch", torch.__version__, "cuda:", torch.cuda.is_available())'

for t in "${MODEL_TYPES[@]}"; do
  mkdir -p "$MODELS_DIR/$t"
done
mkdir -p "$DATA_DIR/output" "$DATA_DIR/comfy/input" "$DATA_DIR/comfy/output"

# Seed settings once so the web installer is skipped; never overwrite changes made in the UI.
# SwarmUI writes comfy-auto-model.yaml from Paths, so ComfyUI sees the same shared models.
mkdir -p "$SRC_DIR/Data"
if [ ! -f "$SRC_DIR/Data/Settings.fds" ]; then
  cat > "$SRC_DIR/Data/Settings.fds" <<SETTINGS
IsInstalled: true
InstallDate: $(date +%F)
InstallVersion: $SWARM_VERSION
LaunchMode: none
Network:
    Host: *
    PortCanChange: false
Paths:
    ModelRoot: $MODELS_DIR
    SDModelFolder: checkpoints
    SDLoraFolder: loras
    SDVAEFolder: vae
    SDEmbeddingFolder: embeddings
    OutputPath: $DATA_DIR/output
SETTINGS
fi

if [ ! -f "$SRC_DIR/Data/Backends.fds" ]; then
  cat > "$SRC_DIR/Data/Backends.fds" <<BACKENDS
0:
    type: comfyui_selfstart
    title: ComfyUI
    enabled: true
    settings:
        StartScript: $COMFY_DIR/main.py
        ExtraArgs: --input-directory $DATA_DIR/comfy/input --output-directory $DATA_DIR/comfy/output
        AutoUpdate: false
        GPU_ID: 0
BACKENDS
fi

# The seeded Host of "*" binds both families for the dual-stack swarmui service; it can't go on
# the command line because launch-linux.sh passes its args on unquoted and the shell would glob it.
# launch-linux.sh builds SwarmUI when the checkout has moved and relaunches it when it exits to
# restart (code 42).
exec "$SRC_DIR/launch-linux.sh" --launch_mode none --port "$LISTEN_PORT"
