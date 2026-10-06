#!/bin/bash
# Install (on first run or version change) and launch Stable Diffusion WebUI Forge on the shared
# ai-applications volume. The ai-base image has no python, so uv bootstraps a standalone
# interpreter and venv on the volume. Forge has no release tags, so FORGE_VERSION is a commit;
# bump it to upgrade. Extensions in the checkout are kept.
# Models live in MODELS_DIR (shared with the other AI apps), passed in as Forge's *-dir args.
# Generated images live in DATA_DIR, outside the checkout.
set -euo pipefail

APP_DIR="${APP_DIR:-/srv/ai/forge}"
FORGE_VERSION="${FORGE_VERSION:-dfdcbab685e57677014f05a3309b48cc87383167}"
FORGE_REPO="${FORGE_REPO:-https://github.com/lllyasviel/stable-diffusion-webui-forge.git}"
# Forge's pinned requirements (numpy 1.26, Pillow 9.5) are tested on 3.10 only
PYTHON_VERSION="${PYTHON_VERSION:-3.10}"
# Forge was last updated against torch 2.7; cu128 is the oldest backend that runs on Blackwell
TORCH_BACKEND="${TORCH_BACKEND:-cu128}"
TORCH_PACKAGES="${TORCH_PACKAGES:-torch==2.7.1 torchvision==0.22.1}"
CLIP_PACKAGE="${CLIP_PACKAGE:-https://github.com/openai/CLIP/archive/d50d76daa670286dd6cacf3bcd80b5e4823fc8e1.zip}"
# not FORGE_PORT: kubernetes sets that to tcp://<ip>:7860 for the forge service
LISTEN_PORT="${LISTEN_PORT:-7860}"
MODELS_DIR="${MODELS_DIR:-/srv/models}"
DATA_DIR="${DATA_DIR:-/srv/data/forge}"

SRC_DIR="$APP_DIR/stable-diffusion-webui-forge"
VENV_DIR="$APP_DIR/venv-py$PYTHON_VERSION"
STAMP="$VENV_DIR/.forge-version"
# Forge model dir arg -> shared (ComfyUI-named) folder
MODEL_ARGS=(ckpt-dir:checkpoints vae-dir:vae lora-dir:loras embeddings-dir:embeddings
  controlnet-dir:controlnet hypernetwork-dir:hypernetworks esrgan-models-path:upscale_models
  text-encoder-dir:text_encoders)

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

# Clone on first run, otherwise move the existing checkout to the pinned commit
if [ ! -d "$SRC_DIR/.git" ]; then
  echo "Cloning Forge $FORGE_VERSION into $SRC_DIR"
  git clone "$FORGE_REPO" "$SRC_DIR"
  git -C "$SRC_DIR" checkout --force "$FORGE_VERSION"
elif [ "$(git -C "$SRC_DIR" rev-parse HEAD)" != "$FORGE_VERSION" ]; then
  echo "Updating Forge checkout to $FORGE_VERSION"
  git -C "$SRC_DIR" fetch --force origin
  git -C "$SRC_DIR" checkout --force "$FORGE_VERSION"
fi

# --seed adds pip: launch.py pip-installs the requirements of built-in and user extensions
if [ ! -x "$VENV_DIR/bin/python" ]; then
  uv venv --seed --python "$PYTHON_VERSION" "$VENV_DIR"
fi

# (Re)install dependencies only when the pinned version changes. launch.py would do this with
# pip, but it installs torch from cu121 and builds CLIP in an isolated env with a current
# setuptools, which no longer ships the pkg_resources CLIP's setup.py needs; building it against
# the venv's pinned setuptools works.
if [ "$(cat "$STAMP" 2>/dev/null || true)" != "$FORGE_VERSION" ]; then
  echo "Installing Forge $FORGE_VERSION dependencies"
  export VIRTUAL_ENV="$VENV_DIR"
  # shellcheck disable=SC2086 # TORCH_PACKAGES is a list
  uv pip install --torch-backend "$TORCH_BACKEND" $TORCH_PACKAGES \
    -r "$SRC_DIR/requirements_versions.txt"
  uv pip install --no-build-isolation "$CLIP_PACKAGE"
  echo "$FORGE_VERSION" > "$STAMP"
fi

"$VENV_DIR/bin/python" -c 'import torch; print("torch", torch.__version__, "cuda:", torch.cuda.is_available())'

model_args=()
for m in "${MODEL_ARGS[@]}"; do
  mkdir -p "$MODELS_DIR/${m##*:}"
  model_args+=("--${m%%:*}" "$MODELS_DIR/${m##*:}")
done
mkdir -p "$DATA_DIR"

# Seed settings once so images are saved to DATA_DIR and no browser launch is attempted; never
# overwrite changes made in the UI
if [ ! -f "$SRC_DIR/config.json" ]; then
  cat > "$SRC_DIR/config.json" <<SETTINGS
{
  "auto_launch_browser": "Disable",
  "outdir_txt2img_samples": "$DATA_DIR/outputs/txt2img-images",
  "outdir_img2img_samples": "$DATA_DIR/outputs/img2img-images",
  "outdir_extras_samples": "$DATA_DIR/outputs/extras-images",
  "outdir_txt2img_grids": "$DATA_DIR/outputs/txt2img-grids",
  "outdir_img2img_grids": "$DATA_DIR/outputs/img2img-grids",
  "outdir_save": "$DATA_DIR/log/images",
  "outdir_init_images": "$DATA_DIR/outputs/init-images"
}
SETTINGS
fi

# Gradio hands its server name to uvicorn, and asyncio only binds both 0.0.0.0 and [::] (for the
# dual-stack forge service) when the host is empty. --listen/--server-name can't give it an empty
# name, so leave both off and blank Gradio's default instead.
export GRADIO_SERVER_NAME=""

cd "$SRC_DIR"
# launch.py still clones Forge's helper repos and installs extension requirements before starting
exec "$VENV_DIR/bin/python" launch.py --port "$LISTEN_PORT" --skip-python-version-check \
  "${model_args[@]}" --gradio-allowed-path "$DATA_DIR"
