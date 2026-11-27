# App notes

## Plex

- Media is a read-only static CephFS PV, `plex-nextcloud-media`, of Nextcloud's
  data volume, mounted at `/data/nextcloud`. The pod uses `supplementalGroups: [82]`
  (Nextcloud's fsGroup). Add and organize files in Nextcloud; writing straight
  to CephFS leaves Nextcloud stale until `occ files:scan`.
- Claim token is OpenBao `kv/apps/plex/plex-secrets`, key `PLEX_CLAIM`. To claim:
  `kubectl -n plex port-forward svc/plex-plex-media-server 32400:32400` and open
  http://localhost:32400/web. Auth is plex.tv, not Keycloak.
- GPU is required (`nvidia.com/gpu` + RuntimeClass `nvidia`). Affinity prefers
  w4's RTX 3060 (`nvidia.com/gpu.product=NVIDIA-GeForce-RTX-3060-SHARED`) so w5
  stays free for AI; it can still fall back to w5.
- The ApplicationSet looks up `plex-svc.clt.milco.casa` (MetalLB IP for
  `plex-share`). That name needs the dnsmasq host-record in
  `roles/openwrt/templates/dnsmasq.conf.j2`. Apply OpenWrt DNS before the
  appset can generate the Application. `plex.clt.milco.casa` comes from the
  Ingress via ExternalDNS.

## ExternalDNS

- Writes `domain`/`txtrecord` sections to the router's `/etc/config/dhcp`
  through the LuCI RPC webhook. The openwrt role's `dhcp.j2` overwrites that
  file, so a rerun drops them until the next sync (1m) recreates and commits
  them.
- An apply cut off before `uci commit` leaves records only in the webhook's
  rpcd session (`/var/run/rpcd/uci-*/dhcp`). The webhook still sees them,
  dnsmasq does not, and external-dns logs "up to date". Fix: restart the
  deployment so the webhook logs in to a fresh session.
- The webhook logs in to the router as rpcd user `luci-dns`
  (`roles/openwrt/templates/rpcd.j2`, password `vault_luci_dns_password`, same
  value as OpenBao `kv/apps/external-dns/external-dns-secrets` key
  `openwrt-password`). Its ACL group allows uci `dhcp` only, but LuCI RPC
  (`/cgi-bin/luci/rpc/sys`, `/fs`) accepts any logged-in user and ignores
  rpcd ACLs, so this login is root-equivalent until the webhook moves to `/ubus`.
- `rpcd.j2` owns all of `/etc/config/rpcd`; it also keeps the stock `root`
  login (`$p$root`) for LuCI.
- v0.23 ignores `external-dns.alpha.kubernetes.io/*`. Service hostnames
  (traefik, traefik-ssh, plex-svc) come from `dnsmasq.conf.j2`.

## AI

- Application `ai-applications` ignores `/spec/replicas` on Deployments and
  StatefulSets (`RespectIgnoreDifferences=true` in `argo-apps/appsets/ai.yaml`).
  `kubectl scale -n ai deploy/<name> --replicas=N` survives Argo auto-sync.
- New workloads still start at the replica count in git (usually 1).
- Open WebUI has a PDB (`minAvailable: 1`): scaling to 0 works, but while it runs it blocks node drains.

## AI trainers

ai-toolkit and sd-scripts (kohya) run as Deployments in `ai`. Training
configs, recipes, and prompts do not belong in this repo.

- Stop ComfyUI before training (scale to 0); a 16 GB GPU can't hold both.
- Blackwell (sm_120) needs cu128+ torch. ai-toolkit pins cu130 and Python 3.12
  (scipy pin has no 3.13 wheels). sd-scripts uses branch `main`; skip its
  README torch 2.6/cu124 pin.
- ai-toolkit Flux needs the diffusers folder, not the ComfyUI single file:
  `/srv/models/diffusers/FLUX.1-Krea-dev` (gated, HF read token, ~33 GB).
  Output: `/srv/models/loras/aitk`. UI port 8675 needs `AI_TOOLKIT_AUTH` if exposed.
- kohya uses the ComfyUI files under `/srv/models` (Flux UNet, clip_l, t5xxl,
  ae). Output: `/srv/models/loras/kohya`. Caches go next to the training
  images, so that folder must be writable.
- Run: `kubectl -n ai exec -it deploy/<sd-scripts|ai-toolkit> -- bash`, then
  the run script under `/srv/ai/<app>/config/`.

## Nextcloud

- Preferred node affinity: high, then medium. Can schedule anywhere else
  (data is on CephFS RWX).

## Harbor

- Registry and jobservice use `updateStrategy.type: Recreate` because their
  PVCs are RWO ceph-block.
- Internal DB password is OpenBao `kv/apps/harbor/harbor-secrets`, key
  `password` (same ExternalSecret as the admin password and secretKey).
  Chart 1.19.4 has no `database.internal.existingSecret`; core/exporter use
  `database.external.existingSecret` while type stays internal, and the DB
  pod gets `POSTGRES_PASSWORD` via `extraEnvVars`. The chart still emits
  `harbor-database` with its default password; that value is unused at runtime.

## Gitea

- Valkey password is OpenBao `kv/apps/gitea/gitea-valkey`, key
  `valkey-password`. ExternalSecret `gitea-valkey` also templates `CONN_STR`
  for cache/session/queue (`GITEA__CACHE__HOST`, `GITEA__SESSION__PROVIDER_CONFIG`,
  `GITEA__QUEUE__CONN_STR`). The valkey subchart uses `auth.existingSecret`.
  Set `valkey.global.valkey.password` to empty so the parent chart does not
  bake its default into `gitea-inline-config`.
- Actions runners: three releases of the `actions` chart, each with its own
  dind and a 50Gi `docker-data` emptyDir. Each runs on its own medium
  worker (required pod anti-affinity) and has CPU and memory limits.
  Placement prefers non-GPU nodes and falls back to a GPU medium worker
  when one is out.
  - `gitea-actions` (`actions-values.yaml`): global token, labels `ubuntu-*`,
    4 slots. It runs PR jobs, so jobs get no Docker socket (`docker_host: "-"`)
    and can't use `docker build` or `docker run`.
  - `gitea-actions-build` (`actions-build-values.yaml` on top of it): label
    `docker-build`, 1 slot, jobs get the socket. Only `ai-base` uses it, never
    on `pull_request`; `scripts/check-runner-labels.py` fails pr-checks
    otherwise, and on any workflow here using `docker`. The
    token is repo-scoped to `homelab/bare-metal-kubernetes`. Mint it in the
    repo's Settings > Actions > Runners and store it in OpenBao at
    `kv/apps/gitea/gitea-runner-build-token`, key `token`.
  - `gitea-actions-docker` (`actions-docker-values.yaml`): label `docker`,
    1 slot, its own dind, jobs get the socket, no secrets. For app repos
    whose CI needs Docker (`runs-on: docker`). Org-scoped to `homelab`; token
    in OpenBao at `kv/apps/gitea/gitea-runner-docker-token`, key `token`.
    Gitea gives a job to whichever matching runner polls first, so it can't
    reuse `ubuntu-*`. Use it only for jobs triggered by `push` to the repo's
    default branch, never `pull_request`: anyone who controls the job controls
    a privileged dind. Keep lint and tests on `ubuntu-*`.
  - Build and docker runners run `post-task.sh`
    (`runner-post-task-configmap.yaml`) after every job. It removes all of
    dind's containers, images, volumes and build cache before the next job,
    so builds start cold. It doesn't help once a job escapes dind to the node.
  - All runners use ServiceAccount `gitea-actions-runner`, with no token
    automount.
  - CiliumNetworkPolicy `gitea-actions-runner` (`runner-network-policy.yaml`)
    lets the runners reach only DNS, Traefik and public addresses. It denies
    CiliumCIDRGroup `gitea-actions-runner-deny`, the LAN's public IPv6 /64,
    which the `gitea` role creates (`--tags gitea`) because the prefix is
    vaulted. The router's public WAN addresses stay reachable.
  - Runner, dind and job images are pinned as `tag@digest`, and a custom
    manager in `renovate.json` updates them.
- Commit signing: Gitea signs web UI commits, merges and API commits as
  `BotMilco` with an SSH key so GitHub shows them verified. OpenBao
  `kv/apps/gitea/gitea-signing-key` holds `privateKey` (OpenSSH, no passphrase),
  `publicKey` and `email`. Add the public key to the GitHub account with
  `email` verified, as a Signing key.

## Renovate

Renovate runs hourly from the Gitea repo `homelab/renovate` on the Actions
runner, as `svc_renovate`, against every repo in the `homelab` org. Releases
younger than 48 hours never get a PR. Repos can't override that.

This repo's `renovate.json` covers Argo CD Helm charts
(`argo-apps/appsets`, `argo-apps/bootstrap`), images in `argo-apps/apps`, and
pins in `hosts/group_vars/all/versions.yaml`. Each pin there needs a
`# renovate: datasource=... depName=...` comment on the line above it; a pin
without one is ignored. A docker pin can carry its digest as
`tag@sha256:...` (see `kube_vip_version`); Renovate updates both. Minor
and major bumps of Kubernetes, CRI-O, Ceph, Rook and Cilium wait for
approval on the Dependency Dashboard issue, because they go through
`upgrade.yaml`.

Updates are grouped into one PR per type: Python packages, Ansible
collections and roles, container images, CI actions, Argo CD charts, Argo CD
app images, and `versions.yaml` pins. Those five upgrade.yaml components and
the Gitea server image keep their own PRs. Major bumps still get a separate
PR per group.

Renovate PRs open on the Gitea copy of this repo. Merge there, then pull and
push to GitHub.

### Renovate review

`.gitea/workflows/renovate-review.yaml` runs `scripts/renovate_review.py`
from `master` after each `pr-checks` run (`workflow_run`), daily at 9:00 ET,
and by hand (optional PR number). It clones `master` into a digest-pinned
`python` image (no setup-python, so nothing from the runner's shared tool
cache) and reads PRs only through the API.

- Gate: open, non-draft PR by `svc_renovate` from a `renovate/*` branch of
  this repo, every commit authored, committed and signed by `svc_renovate`,
  and every `pr-checks / *` status on that head SHA is success. Files and
  commits are read for that SHA. Label `bot/skip` skips a PR.
- Rating: the highest match wins, and anything not known to be low is at
  least medium. Low is a patch or digest of an Argo CD app image or chart
  outside the critical set (traefik, argo-cd, openbao, external-secrets,
  cert-manager, keycloak, lldap). Medium: minor bumps, critical-set patches,
  `versions.yaml` pins, Python packages and Ansible collections, the Actions
  runner, secret/RBAC/policy/ingress files.
  High: major bumps, minor bumps of Kubernetes, CRI-O, Ceph, Rook and Cilium,
  and changes to `.gitea/`, `scripts/`, `renovate.json`,
  lint configs, `.secrets.baseline`, `requirements-ci.*`, OpenBao files and
  the kubernetes, crio, cilium, rook and kube-vip roles. A `risk-floor/<level>`
  label can raise the rating; nothing lowers it.
- Output: one comment per PR, marked `<!-- grokbot-review sha=... -->`, edited
  in place when the rating changes. Quoted text can't @mention or link.
- Setup: repo Actions secret `SVC_GROKBOT_TOKEN`, an `svc_grokbot` token with
  `read:repository` and `write:issue`, and repo Actions variable
  `REVIEW_MODE=comment`.
- Kill switch: set `REVIEW_MODE` to `off` or delete it. `approve` and `fix`
  are not enabled yet and do nothing.
- Local dry run (prints the comments, posts nothing):
  `python3 scripts/renovate_review.py --dry-run --token-file <read-only token file>`.
  Tests: `python3 -m unittest discover -s scripts -p 'test_*.py'`.

## Test apps

Side projects deploy for testing from the Gitea org `homelab`
(https://git.clt.milco.casa). ApplicationSet `test-apps`
(`argo-apps/appsets/test-apps.yaml`) uses the Gitea SCM provider and picks a
repo when both are true:

- It has the Gitea **label** `homelab-test` (Issues > Labels). Argo CD's Gitea
  provider matches repo labels, not topics; a topic alone is ignored.
- It has `deploy/homelab.yaml` on the default branch.

The two checks are separate ApplicationSet filters, not one. Combining
`labelMatch` with `pathsExist` in a single filter makes Argo treat it as
branch-scoped, so it looks up the default branch of every repo in the org
before applying the label. If that branch is missing (Gitea default is
`main` but the repo only has `master`), the whole ApplicationSet fails.
The Gitea default branch must exist on any labeled repo; fix it in the
repo's settings if it does not.

Contract file, `deploy/homelab.yaml`:

```yaml
namespace: myapp-testing        # must match *-testing
sources:
  - path: deploy/helm/myapp     # Helm chart dir
    type: helm
    valueFiles: [values-testing.yaml]  # optional, relative to path
    releaseName: myapp                 # optional, default <repo>
  - path: deploy/postgres       # Kustomize dir
    type: kustomize             # namespace is forced to `namespace`
```

Top-level keys must not collide with generator params (`path`, `url`,
`branch`, `repository`, `labels`, ...). Any other `type` fails the whole
ApplicationSet, so fix it fast.

- Application name: `<repo>-testing`. Auto-sync with prune,
  `CreateNamespace=true`, default branch.
- AppProject `test-apps` (`argo-apps/bootstrap/test-apps.yaml`): destinations
  `*-testing` only (Go filepath.Match); the only cluster-scoped resource is a
  Namespace matching that glob. No `SecretStore`, `Role`, `RoleBinding`,
  `Endpoints`, `EndpointSlice`, or `traefik.io`, `gateway.networking.k8s.io`,
  and `cilium.io` kinds.
  Sources `https://git.clt.milco.casa/homelab/*` and
  `git@git.clt.milco.casa:homelab/*`.
- Namespaces get `pod-security.kubernetes.io/enforce: restricted` and
  `homelab.milco.casa/testing: "true"` (`managedNamespaceMetadata`).
  ValidatingAdmissionPolicy `testing-namespace-labels`
  (`argo-apps/apps/utils/testing-namespace-policy.yaml`) rejects any `*-testing`
  namespace without them. Pods must meet the restricted profile.
- Services: `ClusterIP` only, no `externalIPs`, no external-dns annotations
  (ValidatingAdmissionPolicy `testing-services`). MetalLB `primary-ip-pool`
  also skips namespaces with the testing label.
- Argo clones over **HTTPS** (`https://git.clt.milco.casa/homelab/<repo>`),
  though people use SSH. HTTPS uses the Let's Encrypt wildcard and the same
  token as the API. SSH would need the Gitea host key in Argo's
  `configs.ssh.extraHosts` plus a deploy key.
- Credentials: OpenBao `kv/apps/argocd/gitea-homelab`, keys `username` and
  `token`. Use a dedicated Gitea user or token with read-only scopes
  `read:organization`, `read:repository`, `read:issue` (labels). ExternalSecrets
  in `argo-apps/bootstrap/gitea-homelab-creds.yaml` make `gitea-homelab-repo-creds`
  (repo-creds template for `https://git.clt.milco.casa/homelab`) and
  `gitea-homelab-scm-token` (`scm-creds`, key `token`).
- Ingress: `ingressClassName: traefik`, and every `rules[].host` and
  `tls[].hosts[]` must be `<namespace>.clt.milco.casa` (e.g.
  `myapp-testing.clt.milco.casa`). No `defaultBackend`, no external-dns
  annotations, entry points `web`/`websecure` only, and no
  `traefik.ingress.kubernetes.io/*` annotation besides `router.entrypoints` and
  `router.tls`
  (ValidatingAdmissionPolicy `testing-ingresses`). external-dns publishes the
  host. The wildcard cert is not reflected into `*-testing` namespaces; leave
  `tls.secretName` unset and Traefik serves its default cert
  (`wildcard.milco.casa`, covers `*.clt.milco.casa`).
- Images go in Harbor. A pull-only robot for the app's Harbor project goes in
  OpenBao `kv/apps/<namespace>/harbor-<project>`, keys `username` and `password`.
  The app's `deploy/` creates ServiceAccount `openbao-eso` and an ExternalSecret
  against ClusterSecretStore `openbao-testing` that writes it as a
  `kubernetes.io/dockerconfigjson` Secret. That store only reads
  `kv/apps/<own namespace>/*`; store `openbao` is closed to `*-testing`.
  Docker Hub pulls should go through a Harbor proxy-cache project, which is set
  up in Harbor, not here.
- Live setup is manual: Gitea org, label, token, Harbor project and robot,
  and the OpenBao entries. Apply `argo-apps/bootstrap` with the `argo-apps`
  tag. The ApplicationSet syncs from `argo-apps/appsets`.
