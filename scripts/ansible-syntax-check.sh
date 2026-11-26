#!/usr/bin/env bash
# Syntax-check every top-level playbook. Requires ANSIBLE_INVENTORY and
# ANSIBLE_VAULT_PASSWORD_FILE (a dummy file is enough; vaults/ is gitignored).
set -euo pipefail
fail=0
shopt -s nullglob
for playbook in *.yaml; do
  case "${playbook}" in
    requirements.yml|requirements.yaml) continue ;;
  esac
  echo "syntax-check ${playbook}"
  if ! ansible-playbook --syntax-check "${playbook}"; then
    fail=1
  fi
done
exit "${fail}"
