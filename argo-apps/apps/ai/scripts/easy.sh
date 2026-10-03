#!/bin/bash
# Install (on first run) and launch Easy Diffusion on the shared ai-applications volume.
# The official installer bootstraps its own conda/python env, clones the app, installs
# modules and self-updates on every start, so after the first run this just launches it.
# Its model folders are symlinks into MODELS_DIR, which is shared with the other AI apps.
set -euo pipefail

APP_DIR="${APP_DIR:-/srv/ai/easy}"
INSTALLER_URL="${INSTALLER_URL:-https://github.com/easydiffusion/easydiffusion/releases/latest/download/Easy-Diffusion-Linux.zip}"
MODELS_DIR="${MODELS_DIR:-/srv/models}"
# easy diffusion folder name -> shared (ComfyUI-named) folder
MODEL_LINKS=(stable-diffusion:checkpoints vae:vae lora:loras embeddings:embeddings
  controlnet:controlnet hypernetwork:hypernetworks realesrgan:upscale_models)

mkdir -p "$APP_DIR"
cd "$APP_DIR"

# Download and unpack the installer on first run only
if [ ! -x "$APP_DIR/start.sh" ]; then
  echo "Easy Diffusion not found in $APP_DIR, downloading installer from $INSTALLER_URL"
  tmp=$(mktemp -d)
  curl -fL --retry 3 -o "$tmp/easy-diffusion.zip" "$INSTALLER_URL"
  unzip -q "$tmp/easy-diffusion.zip" -d "$tmp/extract"
  cp -a "$tmp"/extract/easy-diffusion/. "$APP_DIR"/
  chmod +x "$APP_DIR"/*.sh "$APP_DIR"/scripts/*.sh
  rm -rf "$tmp"
fi

# Seed a headless, network-listening config once; never overwrite user changes
if [ ! -f "$APP_DIR/config.yaml" ]; then
  cat > "$APP_DIR/config.yaml" <<'CONFIG'
net:
  bind_ip: ""
  listen_port: 9000
  listen_to_network: true
render_devices: auto
ui:
  open_browser_on_start: false
update_branch: main
CONFIG
fi

# An empty bind_ip makes uvicorn listen on both 0.0.0.0 and [::] for the dual-stack easy service
# (the default 0.0.0.0 is IPv4 only); add it to configs seeded before this was set
if ! grep -q '^  bind_ip:' "$APP_DIR/config.yaml"; then
  sed -i 's/^net:$/net:\n  bind_ip: ""/' "$APP_DIR/config.yaml"
fi

# Replace each model folder with a symlink into the shared dir, moving anything already
# downloaded there first. -n never overwrites a shared file (and exits non-zero when it skips
# one, hence the || true); on a name clash the leftovers are kept in <folder>.unshared for
# manual cleanup.
mkdir -p "$APP_DIR/models"
for link in "${MODEL_LINKS[@]}"; do
  src="$APP_DIR/models/${link%%:*}"
  dst="$MODELS_DIR/${link##*:}"
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

exec "$APP_DIR/start.sh"
