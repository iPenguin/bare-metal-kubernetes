#!/bin/bash
# Install (on first run) and launch Easy Diffusion on the shared ai-applications volume.
# The official installer bootstraps its own conda/python env, clones the app, installs
# modules and self-updates on every start, so after the first run this just launches it.
set -euo pipefail

APP_DIR="${APP_DIR:-/srv/ai/easy}"
INSTALLER_URL="${INSTALLER_URL:-https://github.com/easydiffusion/easydiffusion/releases/latest/download/Easy-Diffusion-Linux.zip}"

mkdir -p "$APP_DIR"
cd "$APP_DIR"

# Download and unpack the installer on first run only
if [ ! -x "$APP_DIR/start.sh" ]; then
  echo "Easy Diffusion not found in $APP_DIR, downloading installer from $INSTALLER_URL"
  tmp=$(mktemp -d)
  curl -fL --retry 3 -o "$tmp/easy-diffusion.zip" "$INSTALLER_URL"
  # the image has no unzip
  python3 -m zipfile -e "$tmp/easy-diffusion.zip" "$tmp/extract"
  cp -a "$tmp"/extract/easy-diffusion/. "$APP_DIR"/
  chmod +x "$APP_DIR"/*.sh "$APP_DIR"/scripts/*.sh
  rm -rf "$tmp"
fi

# Seed a headless, network-listening config once; never overwrite user changes
if [ ! -f "$APP_DIR/config.yaml" ]; then
  cat > "$APP_DIR/config.yaml" <<'CONFIG'
net:
  listen_port: 9000
  listen_to_network: true
render_devices: auto
ui:
  open_browser_on_start: false
update_branch: main
CONFIG
fi

exec "$APP_DIR/start.sh"
