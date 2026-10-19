#!/bin/bash
# Install (on first run or version change) and launch ostris/ai-toolkit (LoRA trainer) and its web
# UI on the shared ai-applications volume. ai-base has no python or node, so uv bootstraps a
# standalone Python 3.12 venv and the script unpacks a Node release next to it.
# ai-toolkit has no release tags, so AI_TOOLKIT_VERSION is a commit; bump it to upgrade.
# Configs and datasets live on the volume; output goes wherever a job's training_folder points
# (e.g. /srv/models/loras/aitk so ComfyUI sees the LoRAs). See docs/ai-toolkit.md.
# The checkout's datasets/ (input) and output/ folders, the UI's defaults, are symlinks into DATA_DIR.
set -euo pipefail

APP_DIR="${APP_DIR:-/srv/ai/ai-toolkit}"
AI_TOOLKIT_VERSION="${AI_TOOLKIT_VERSION:-8cf43dd}"
AI_TOOLKIT_REPO="${AI_TOOLKIT_REPO:-https://github.com/ostris/ai-toolkit.git}"
# requirements.txt pins scipy==1.12.0, which has no cp313 wheels; upstream's manager pins 3.12
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
# README pins; Blackwell (sm_120) needs the cu130 wheels
TORCH_BACKEND="${TORCH_BACKEND:-cu130}"
TORCH_PACKAGES="${TORCH_PACKAGES:-torch==2.13.0 torchvision==0.28.0 torchaudio==2.11.0}"
# the UI needs Node > 20
NODE_VERSION="${NODE_VERSION:-22.20.0}"
LISTEN_PORT="${LISTEN_PORT:-8675}"
DATA_DIR="${DATA_DIR:-/srv/data/ai-toolkit}"

SRC_DIR="$APP_DIR/ai-toolkit"
# the UI looks for the trainer's venv at <checkout>/venv or <checkout>/.venv
VENV_DIR="$SRC_DIR/venv"
STAMP="$VENV_DIR/.ai-toolkit-version"
NODE_DIR="$APP_DIR/node-v$NODE_VERSION"

export UV_INSTALL_DIR="$APP_DIR/bin"
export UV_PYTHON_INSTALL_DIR="$APP_DIR/python"
export UV_CACHE_DIR="$APP_DIR/.cache/uv"
export npm_config_cache="$APP_DIR/.cache/npm"
export HF_HOME="${HF_HOME:-$APP_DIR/.cache/huggingface}"
# cache and venv live on cephfs; don't rely on hardlinks
export UV_LINK_MODE=copy
export PATH="$NODE_DIR/bin:$UV_INSTALL_DIR:$PATH"

mkdir -p "$APP_DIR"
cd "$APP_DIR"

if ! command -v uv >/dev/null; then
  echo "Installing uv into $UV_INSTALL_DIR"
  curl -fsSL --retry 3 https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
fi

if [ ! -x "$NODE_DIR/bin/node" ]; then
  echo "Installing Node $NODE_VERSION into $NODE_DIR"
  curl -fsSL --retry 3 "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-x64.tar.xz" \
    | tar -xJ -C "$APP_DIR"
  mv "$APP_DIR/node-v$NODE_VERSION-linux-x64" "$NODE_DIR"
fi

# Clone on first run, otherwise move the existing checkout to the pinned commit
if [ ! -d "$SRC_DIR/.git" ]; then
  echo "Cloning ai-toolkit $AI_TOOLKIT_VERSION into $SRC_DIR"
  git clone "$AI_TOOLKIT_REPO" "$SRC_DIR"
  git -C "$SRC_DIR" checkout --force "$AI_TOOLKIT_VERSION"
elif [ "$(git -C "$SRC_DIR" rev-parse --short=7 HEAD)" != "${AI_TOOLKIT_VERSION:0:7}" ]; then
  echo "Updating ai-toolkit checkout to $AI_TOOLKIT_VERSION"
  git -C "$SRC_DIR" fetch --force origin
  git -C "$SRC_DIR" checkout --force "$AI_TOOLKIT_VERSION"
fi
git -C "$SRC_DIR" submodule update --init --recursive

if [ ! -x "$VENV_DIR/bin/python" ]; then
  uv venv --seed --python "$PYTHON_VERSION" "$VENV_DIR"
fi

# (Re)install python deps and rebuild the UI only when the pinned version changes
if [ "$(cat "$STAMP" 2>/dev/null || true)" != "$AI_TOOLKIT_VERSION" ]; then
  echo "Installing ai-toolkit $AI_TOOLKIT_VERSION dependencies"
  export VIRTUAL_ENV="$VENV_DIR"
  # shellcheck disable=SC2086 # TORCH_PACKAGES is a list
  uv pip install --torch-backend "$TORCH_BACKEND" $TORCH_PACKAGES
  uv pip install -r "$SRC_DIR/requirements.txt"
  (cd "$SRC_DIR/ui" && npm install && npm run update_db && npm run build)
  echo "$AI_TOOLKIT_VERSION" > "$STAMP"
fi

# Keep datasets (input) and training runs (output) in DATA_DIR, outside the checkout, like the
# other AI apps. Anything already in the checkout is moved there first; -n never overwrites (and
# exits non-zero when it skips, hence the || true); on a name clash the leftovers stay in
# <folder>.unshared for manual cleanup.
for d in datasets:input output:output; do
  src="$SRC_DIR/${d%%:*}"
  dst="$DATA_DIR/${d##*:}"
  mkdir -p "$dst"
  [ -L "$src" ] && continue
  if [ -d "$src" ]; then
    find "$src" -mindepth 1 -maxdepth 1 -exec mv -n -t "$dst" {} + || true
    if ! rmdir "$src" 2>/dev/null; then
      echo "WARNING: $src has files that already exist in $dst, keeping them in $src.unshared"
      mv "$src" "$src.unshared"
    fi
  fi
  ln -s "$dst" "$src"
done

"$VENV_DIR/bin/python" -c 'import torch; print("torch", torch.__version__, "cuda:", torch.cuda.is_available())'

if [ -z "${AI_TOOLKIT_AUTH:-}" ]; then
  echo "WARNING: AI_TOOLKIT_AUTH is not set; the UI is open to anyone who can reach it"
fi

cd "$SRC_DIR/ui"
exec npm run start -- --port "$LISTEN_PORT"
