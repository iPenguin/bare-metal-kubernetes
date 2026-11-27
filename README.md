# Bare Metal Kubernetes Cluster

## Description

This repository contains the needed elements to setup a bare metal cluster using ansible.

This repo does basic configuration for the following components:

- **[Ansible](https://www.ansible.com/)** - for automation of deployment
- **[Kubernetes](https://kubernetes.io/)** - for container orchestration
- **[Cilium](https://www.cilium.io/)** - as the CNI
- **[MetalLB](https://metallb.io/)** - for load balancing
- **[Rook](https://rook.io/)/[Ceph](https://ceph.io/)** - for automatic provisioning of storage including s3 buckets
- **[Helm](https://helm.sh/)** - for software management and installation
- **[ArgoCD](https://argo-cd.readthedocs.io/)** - For deployment management
- **Kubernetes [metrics-server](https://github.com/kubernetes-sigs/metrics-server)** - for basic resource monitoring
- **[Prometheus](https://prometheus.io/)/[Grafana](https://grafana.com/)** - for advanced resource monitoring and visualization
- **[LLDAP](https://www.lldap.com/)** - For centralized Identinty Management
- **[OpenBao](https://openbao.org/)** - For app secrets (External Secrets). Sealed Secrets remains only for a few leftover apps

## Setup

If you are starting with a clean working environment you can do the following to boot
strap your working environment on your local computer or in a
[distrobox container](https://distrobox.privatedns.org/):

The setup sript will install a few basic apt and pip packages needed to run the ansible
scripts to setup the workstation. The setup script will also setup the pre-commit hooks
for this repo.

```sh
eval $(./setup.sh)
ansible-playbook -i hosts/hosts.yaml setup-workstation.yml
ansible-playbook -i hosts/hosts.yaml setup-workstation-dev.yaml
ansible-playbook -i hosts/hosts.yaml setup-workstation-apps.yaml
```

## Remote access

SSH is not forwarded from the WAN; connect over WireGuard. `configure-os` installs
`/etc/ssh/sshd_config.d/10-hardening.conf`: keys only, no root login, and
`AllowUsers` from `sshd_allow_users` (default: the `remote_users` accounts).
`ansible-playbook run.yaml --tags sshd` applies only that part of `configure-os`.

## Lint and CI

`ansible-lint` passes at the `safety` profile (`.ansible-lint`, `.yamllint`). Run it
with `ANSIBLE_INVENTORY=hosts` and a dummy `ANSIBLE_VAULT_PASSWORD_FILE` when the
vault password isn't on the machine. `hosts` is a reserved name, so the host list
in `group_vars/all/network.yaml` is `network_hosts`.

Gitea Actions workflow `pr-checks` (`.gitea/workflows/pr-checks.yaml`) runs on
pull requests and on pushes to `master`. The Python checks (pre-commit,
detect-secrets, ansible-lint, playbook syntax, yamllint, the
`minimumReleaseAge` script, and gitleaks) share one job so they cannot race
`setup-python` on the shared runner toolcache. `renovate-config-validator`
runs in the digest-pinned Renovate image. CI installs
`pip install --require-hashes -r requirements-ci.lock` (regenerate from
`requirements-ci.txt` with `pip-compile --generate-hashes`). gitleaks is SHA-pinned; its allowlists cover
the detect-secrets baseline, SealedSecret ciphertext, and the pending kubeadm
bootstrap token. yamllint uses the repo `.yamllint`, which excludes `argo-apps/`
and `images/`.

## Secrets

New app secrets go in OpenBao at `kv/apps/<namespace>/<secret-name>` and are
synced by an ExternalSecret against ClusterSecretStore `openbao`
(`argo-apps/apps/utils/cluster-secret-store.yaml`); `*-testing` namespaces use
`openbao-testing` instead. Import a live Secret with
`scripts/openbao-import-secret.sh <namespace> <secret-name>`.

Kubeseal is only for the remaining SealedSecrets: `keycloak-passwords`,
`lldap-secrets`, `minio-credentials`, `argo-oidc-secrets`, and
`minio-oidc-credentials`. Do not add new ones.

## Docs

- `docs/openbao-bootstrap.md` — OpenBao stand-up and leftover secret migration
- `docs/apps.md` — Plex, AI scaling, Nextcloud placement, Harbor, Gitea, test apps
- `docs/rook-csi-drivers.md` — Rook 1.20 CSI chart migration
