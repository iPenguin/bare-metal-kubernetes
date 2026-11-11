# OpenBao bootstrap runbook

This document is the one-time operator runbook for standing up OpenBao
on a fresh Raspberry Pi (the unseal helper) and on the Kubernetes cluster.
Subsequent `ansible-playbook` runs converge idempotently.

The architecture:

- **Pi-OpenBao** (`bao1.clt.milco.casa`): systemd service, file storage,
  Transit secrets engine only. Auto-unseals itself at boot from a
  root-owned file containing the Shamir keys.
- **Cluster-OpenBao** (`openbao` namespace, 3 replicas, Raft storage):
  auto-unsealed via the Pi's Transit engine. UI behind Traefik with
  Keycloak OIDC.

## What is automated vs. manual

| Step | Command | Notes |
|---|---|---|
| A. Mint internal CA + Pi cert | one-time (already done) | Certs in `vaults/group_vars/all/openbao.yaml` |
| B. Pi OS provisioning | **manual** | SD card, DHCP, SSH |
| C. Install Pi binary + service | `run.yaml --tags openbao-pi` | |
| D+E. Init Pi + auto-persist secrets | `openbao-pi-init.yaml --tags openbao-pi-init` | Writes `openbao_generated.yaml` |
| F. Wire Pi auto-unseal | `run.yaml --tags openbao-pi` | |
| G. Deploy cluster Helm chart | `run.yaml --tags openbao` | |
| H+I. Init cluster + configure auth | `openbao-cluster-init.yaml --tags openbao-cluster-init` | Appends to `openbao_generated.yaml` |
| Set `openbao_client_secret` | **manual** | Keycloak client secret |
| Set MinIO vault vars | **manual** | Before cluster init |
| J. Smoke tests | **manual** | Verify OIDC + Raft |

All sensitive material ends up encrypted in
`vaults/group_vars/all/openbao_generated.yaml` (ansible-vault strings,
safe to commit). The raw capture files (`.openbao-pi-init.json`,
`.openbao-cluster-init.json`) should be shredded once the vault file is
verified.

---

## A. Mint the internal CA (one-time, already done)

A dedicated long-lived CA is used **only** for Pi <-> cluster mTLS so
the cluster pods can trust `https://bao1.clt.milco.casa:8200` without
depending on Let's Encrypt or cert-manager. The certs are in
`vaults/group_vars/all/openbao.yaml`.

If certs need to be regenerated:

```bash
cd /tmp/openbao-ca
umask 077

# 1. Root CA, 10y, ED25519
openssl genpkey -algorithm ED25519 -out ca.key
openssl req -x509 -new -nodes -key ca.key -days 3650 -out ca.crt \
    -subj "/CN=OpenBao Internal CA/O=clt.milco.casa"

# 2. Pi server key + CSR
openssl genpkey -algorithm ED25519 -out bao1.key
cat > bao1.cnf <<'EOF'
[req]
distinguished_name = dn
req_extensions = v3_req
prompt = no
[dn]
CN = bao1.clt.milco.casa
[v3_req]
subjectAltName = @alt
extendedKeyUsage = serverAuth
[alt]
DNS.1 = bao1.clt.milco.casa
IP.1  = 10.1.1.20
IP.2  = 127.0.0.1
EOF
openssl req -new -key bao1.key -out bao1.csr -config bao1.cnf

# 3. Sign with the CA
openssl x509 -req -in bao1.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
    -out bao1.crt -days 3650 -extfile bao1.cnf -extensions v3_req
```

Encrypt each PEM and paste into `vaults/group_vars/all/openbao.yaml`:

```bash
cd /workspaces/ansible
for var in vault_openbao_internal_ca_cert vault_openbao_internal_ca_key \
           vault_openbao_pi_server_cert vault_openbao_pi_server_key; do
    case "$var" in
        *_internal_ca_cert) f=/tmp/openbao-ca/ca.crt ;;
        *_internal_ca_key)  f=/tmp/openbao-ca/ca.key ;;
        *_server_cert)      f=/tmp/openbao-ca/bao1.crt ;;
        *_server_key)       f=/tmp/openbao-ca/bao1.key ;;
    esac
    ansible-vault encrypt_string --name "$var" "$(cat "$f")"
done
```

Securely destroy `/tmp/openbao-ca` once the values are saved.

---

## B. Provision the Pi OS (manual, out of band)

1. Flash Raspberry Pi OS 64-bit Lite onto the SD card.
2. Set hostname `bao1`, enable SSH, create the `ansible` user with our
   SSH key.
3. Assign static DHCP lease at `10.1.1.20` on the router.
4. Confirm `ssh ansible@bao1.clt.milco.casa` works.

---

## Pre-flight: set MinIO credentials

Before running the cluster init playbook, set the MinIO credentials used
by the daily Raft snapshot CronJob in `vaults/group_vars/all/openbao.yaml`:

```bash
cd /workspaces/ansible
ansible-vault encrypt_string --name vault_openbao_minio_access_key '<access-key>'
ansible-vault encrypt_string --name vault_openbao_minio_secret_key '<secret-key>'
# paste each block into vaults/group_vars/all/openbao.yaml
```

Reuse the Velero MinIO user or create a dedicated one.

---

## Pre-flight: create Keycloak OpenBao client

1. In Keycloak, create a client named `openbao` in the `homelab` realm:
   - Client type: OpenID Connect
   - Valid redirect URIs: `https://bao.clt.milco.casa/*`
   - Groups mapper: add `groups` claim to the token
2. Copy the client secret, then encrypt and set it in
   `hosts/group_vars/all/apps.yaml`:

```bash
ansible-vault encrypt_string --name openbao_client_secret '<client-secret>'
# replace the REPLACE_ME_WITH_ansible-vault_encrypt_string placeholder
```

---

## C. Install Pi binary and service

```bash
ansible-playbook run.yaml --tags openbao-pi
```

The unseal-keyfile task self-skips because `vault_openbao_pi_unseal_keys`
is still empty. The service starts sealed; that is expected.

---

## D+E. Initialize Pi-OpenBao and persist secrets

```bash
ansible-playbook openbao-pi-init.yaml --tags openbao-pi-init
```

On the first run this playbook:

1. Initializes OpenBao (`-key-shares=5 -key-threshold=3`).
2. Saves the raw JSON to `.openbao-pi-init.json` on the control node.
3. Unseals with 3 of the 5 keys.
4. Enables `transit`, creates `transit/keys/autounseal`.
5. Installs the `autounseal` policy and mints the cluster autounseal token.
6. **Encrypts all three secrets** (`vault_openbao_pi_unseal_keys`,
   `vault_openbao_pi_root_token`, `vault_openbao_pi_autounseal_token`)
   with `ansible-vault encrypt_string` and writes them to
   `vaults/group_vars/all/openbao_generated.yaml`.

The manual Section E (editing the vault by hand) is no longer needed.

Verify the generated file looks sane:

```bash
ansible-vault view vaults/group_vars/all/openbao_generated.yaml
```

Then shred the raw capture:

```bash
shred -u .openbao-pi-init.json
```

---

## F. Wire Pi auto-unseal

```bash
ansible-playbook run.yaml --tags openbao-pi
```

This pass writes `/etc/openbao/unseal.key` (root-owned, 0400) and the
unseal helper script. Reboot the Pi and confirm the service comes back
unsealed without intervention:

```bash
ssh ansible@bao1.clt.milco.casa sudo systemctl reboot
# wait ~30s
ssh ansible@bao1.clt.milco.casa \
  "BAO_ADDR=https://127.0.0.1:8200 BAO_CACERT=/etc/openbao/tls/ca.crt bao status"
# Sealed should be false
```

---

## G. Deploy cluster-OpenBao

```bash
ansible-playbook run.yaml --tags openbao
```

Helm installs the chart. Pods come up sealed until cluster init runs.
The Transit auto-unseal token is already wired in from the helm values.

---

## H+I. Initialize cluster-OpenBao and configure auth methods

```bash
ansible-playbook openbao-cluster-init.yaml --tags openbao-cluster-init
```

This playbook:

1. Waits for `openbao-0` to be Running.
2. Runs `bao operator init -recovery-shares=5 -recovery-threshold=3`.
3. Waits for Transit auto-unseal to complete (~30s).
4. Encrypts and appends `vault_openbao_cluster_recovery_keys` and
   `vault_openbao_cluster_root_token` to `openbao_generated.yaml`.
5. Enables auth methods: **AppRole**, **Kubernetes**, **OIDC** (Keycloak).
6. Configures OIDC with the `openbao_client_secret` from `apps.yaml`.
7. Creates policies: `admin`, `eso`, `raft-snapshot`.
8. Creates the ESO Kubernetes role bound to the `external-secrets` SA.
9. Mints the raft-snapshot token and creates both K8s secrets
   (`openbao-raft-snapshot-token`, `openbao-raft-snapshot-minio`).
10. Enables KV v2 at path `kv/`.

All auth method and policy writes are idempotent (PUT semantics). Auth
`enable` calls are guarded to skip if the path is already in use.

Shred the raw capture after verifying the vault file:

```bash
shred -u .openbao-cluster-init.json
```

---

## J. Smoke tests

1. Browse `https://bao.clt.milco.casa` → click "Sign in via OIDC" →
   redirects to Keycloak → back to OpenBao logged in as your user.

2. Check cluster health:
   ```bash
   kubectl -n openbao exec openbao-0 -- bao status
   # HA Enabled: true, Sealed: false, Recovery Seal Type: transit
   kubectl -n openbao exec openbao-0 -- bao operator raft list-peers
   # three voters
   ```

3. Reboot the Pi — cluster pods stay unsealed (token cached), Pi
   re-unseals itself within ~30s.

4. Manually trigger a snapshot to confirm MinIO upload works:
   ```bash
   kubectl -n openbao create job --from=cronjob/openbao-raft-snapshot test-snap
   kubectl -n openbao logs -f job/test-snap
   # should upload to minio-local.clt.milco.casa/velero-backups/openbao-raft-snapshots/
   ```

5. `kubectl get pods -n external-secrets` — ESO controllers Running.
   `kubectl get clustersecretstore openbao` should be Valid. App
   ExternalSecrets sync from `kv/apps/<namespace>/<secret-name>`.

---

## App secrets after bootstrap

ClusterSecretStore `openbao` lives in `argo-apps/apps/utils/cluster-secret-store.yaml`.
The `eso` policy can read `kv/apps/*`. New secrets go there via ExternalSecret,
not Sealed Secrets. Import with `scripts/openbao-import-secret.sh <namespace> <secret-name>`.

Still SealedSecret (do not remove the sealed-secrets controller yet):

- `keycloak/keycloak-passwords`
- `lldap/lldap-secrets`
- `minio/minio-credentials`
- `argocd/argo-oidc-secrets` (Ansible `kubeseal` in `roles/argo`)
- `minio/minio-oidc-credentials` (Ansible `kubeseal` in `roles/minio`)

Not yet done: per-namespace KV policies/roles for in-app service logins,
and `community.hashi_vault` lookups for Ansible itself.
