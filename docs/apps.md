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
