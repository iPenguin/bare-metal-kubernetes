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
  Namespace matching that glob. Sources `https://git.clt.milco.casa/homelab/*`
  and `git@git.clt.milco.casa:homelab/*`.
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
- Apps set their own Ingress. TLS is `wildcard.milco.casa`, reflected into
  namespaces in `cert_namespaces` (`hosts/group_vars/all/main.yaml`). That list
  includes the Emberstack regex `.*-testing`, so any `*-testing` namespace gets
  the cert once the cert-manager role is applied with that value.
- Images go in Harbor. A pull-only robot for the app's Harbor project goes in
  OpenBao `kv/apps/<namespace>/harbor-<project>`, keys `username` and `password`.
  The app's `deploy/` creates a namespaced ExternalSecret that writes it as a
  `kubernetes.io/dockerconfigjson` Secret. Docker Hub pulls should go through a
  Harbor proxy-cache project, which is set up in Harbor, not here.
- Live setup is manual: Gitea org, label, token, Harbor project and robot,
  and the OpenBao entries. Apply `argo-apps/bootstrap` with the `argo-apps`
  tag. The ApplicationSet syncs from `argo-apps/appsets`.
