#!/usr/bin/env bash
# Copy a live Kubernetes Secret into OpenBao at kv/apps/<namespace>/<secret-name>.
# Values go straight from kubectl to bao over stdin and are never printed.
#
# Usage: scripts/openbao-import-secret.sh <namespace> <secret-name>
# It prompts for an OpenBao token: sign in to the UI with OIDC and use
# the user menu -> "Copy token".
set -euo pipefail

ns=${1:?usage: $0 <namespace> <secret-name>}
name=${2:?usage: $0 <namespace> <secret-name>}

read -rsp "OpenBao token: " token
echo >&2

{
  printf '%s\n' "$token"
  kubectl -n "$ns" get secret "$name" -o json | jq -c '.data | map_values(@base64d)'
} | kubectl -n openbao exec -i openbao-0 -- sh -c '
  IFS= read -r BAO_TOKEN
  export BAO_TOKEN BAO_ADDR=http://127.0.0.1:8200
  bao kv put -mount=kv "$0" -
' "apps/$ns/$name"
