#!/bin/bash
# Install (on first run or version change) kohya-ss/sd-scripts (LoRA trainer, CLI only) on the
# shared ai-applications volume, then keep the pod up so training runs with kubectl exec:
#   kubectl -n ai exec -it deploy/sd-scripts -- bash /srv/ai/sd-scripts/config/<run>.sh
# ai-base has no python, so uv bootstraps a standalone interpreter and venv on the volume.
# sd-scripts has no release tags, so SD_SCRIPTS_VERSION is a commit; bump it to upgrade.
# Run configs live in CONFIG_DIR; datasets (input) and training runs (output) live in DATA_DIR,
# outside the checkout, like the other AI apps. See docs/kohya/sd-scripts.md.
set -euo pipefail

APP_DIR="${APP_DIR:-/srv/ai/sd-scripts}"
SD_SCRIPTS_VERSION="${SD_SCRIPTS_VERSION:-11d8cedd0c67b93a10f8b2d75f43554f6f12c794}"
SD_SCRIPTS_REPO="${SD_SCRIPTS_REPO:-https://github.com/kohya-ss/sd-scripts.git}"
# README lists 3.10 and says 3.11/3.12 work
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
# README's torch 2.6/cu124 has no Blackwell (sm_120) kernels; cu128 is the oldest that does
TORCH_BACKEND="${TORCH_BACKEND:-cu128}"
TORCH_PACKAGES="${TORCH_PACKAGES:-torch==2.7.1 torchvision==0.22.1}"
DATA_DIR="${DATA_DIR:-/srv/data/sd-scripts}"

SRC_DIR="$APP_DIR/sd-scripts"
# the run scripts in docs/kohya source $APP_DIR/venv and read $APP_DIR/config
VENV_DIR="$APP_DIR/venv"
CONFIG_DIR="$APP_DIR/config"
STAMP="$VENV_DIR/.sd-scripts-version"
READY="$APP_DIR/.ready"

export UV_INSTALL_DIR="$APP_DIR/bin"
export UV_PYTHON_INSTALL_DIR="$APP_DIR/python"
export UV_CACHE_DIR="$APP_DIR/.cache/uv"
export HF_HOME="${HF_HOME:-$APP_DIR/.cache/huggingface}"
# cache and venv live on cephfs; don't rely on hardlinks
export UV_LINK_MODE=copy
export PATH="$UV_INSTALL_DIR:$PATH"

rm -f "$READY"
mkdir -p "$APP_DIR" "$CONFIG_DIR" "$DATA_DIR/input" "$DATA_DIR/output"
cd "$APP_DIR"

if ! command -v uv >/dev/null; then
  echo "Installing uv into $UV_INSTALL_DIR"
  curl -fsSL --retry 3 https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
fi

# Clone on first run, otherwise move the existing checkout to the pinned commit
if [ ! -d "$SRC_DIR/.git" ]; then
  echo "Cloning sd-scripts $SD_SCRIPTS_VERSION into $SRC_DIR"
  git clone "$SD_SCRIPTS_REPO" "$SRC_DIR"
  git -C "$SRC_DIR" checkout --force "$SD_SCRIPTS_VERSION"
elif [ "$(git -C "$SRC_DIR" rev-parse HEAD)" != "$SD_SCRIPTS_VERSION" ]; then
  echo "Updating sd-scripts checkout to $SD_SCRIPTS_VERSION"
  git -C "$SRC_DIR" fetch --force origin
  git -C "$SRC_DIR" checkout --force "$SD_SCRIPTS_VERSION"
fi

if [ ! -x "$VENV_DIR/bin/python" ]; then
  uv venv --seed --python "$PYTHON_VERSION" "$VENV_DIR"
fi

# (Re)install dependencies only when the pinned version changes. requirements.txt installs the
# checkout itself (-e .), so it runs from SRC_DIR; torch goes last so requirements can't move it.
if [ "$(cat "$STAMP" 2>/dev/null || true)" != "$SD_SCRIPTS_VERSION" ]; then
  echo "Installing sd-scripts $SD_SCRIPTS_VERSION dependencies"
  export VIRTUAL_ENV="$VENV_DIR"
  # shellcheck disable=SC2086 # TORCH_PACKAGES is a list
  uv pip install --torch-backend "$TORCH_BACKEND" $TORCH_PACKAGES
  (cd "$SRC_DIR" && uv pip install -r requirements.txt)
  # shellcheck disable=SC2086
  uv pip install --torch-backend "$TORCH_BACKEND" $TORCH_PACKAGES
  echo "$SD_SCRIPTS_VERSION" > "$STAMP"
fi

# single GPU, bf16, no DeepSpeed; written once, never overwritten
if [ ! -f "$HOME/.cache/huggingface/accelerate/default_config.yaml" ]; then
  "$VENV_DIR/bin/accelerate" config default --mixed_precision bf16
fi

"$VENV_DIR/bin/python" -c 'import torch; print("torch", torch.__version__, "cuda:", torch.cuda.is_available())'

# The checkout has no input/output folders of its own; expose DATA_DIR next to the configs so
# run scripts can use $APP_DIR/input and $APP_DIR/output
for d in input output; do
  [ -L "$APP_DIR/$d" ] || ln -s "$DATA_DIR/$d" "$APP_DIR/$d"
done

touch "$READY"
echo "sd-scripts ready. Start a run with: bash $CONFIG_DIR/<run>.sh"
exec sleep infinity
