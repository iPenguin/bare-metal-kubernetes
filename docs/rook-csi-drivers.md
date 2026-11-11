# Rook 1.20 CSI chart migration

Local configuration change only. No chart installation, Ansible playbook, or
cluster mutation was run while preparing this change. Schedule a storage
maintenance window; changing Driver service accounts rolls node plugins and may
briefly interrupt CSI operations. Never do this concurrently with node upgrades.

## Versions and order

- Verified pair: Rook 1.20.10, bundled ceph-csi-operator 1.0.4, drivers chart 1.0.4.
- Fresh install: `rook-ceph` (wait for CRDs/operator), `ceph-csi-drivers`, then
  `rook-ceph-cluster`, then CSI readiness validation.
- Existing installations: export settings **before** upgrading the operator.
  Rook 1.19 must first be at least 1.19.5; older minor releases must be upgraded
  one supported minor at a time using their matching upstream guides. This role's
  new preflight refuses older releases, downgrades, and unreviewed target pins.
  Use the previous-version configuration to stage older releases, not this
  1.20-specific drivers template. Custom CSI images additionally need the manual
  scale-down/review sequence in upstream docs; do not combine that with node drains.
- No Harbor dependency: Rook remains on `quay.io/rook/ceph`; other CSI images use
  Rook's image-set ConfigMap and upstream public registries.

Sources (pinned):
- https://github.com/rook/rook/blob/v1.20.10/Documentation/Upgrade/rook-upgrade.md
- https://github.com/rook/rook/blob/v1.20.10/Documentation/Helm-Charts/csi-drivers-chart.md
- https://github.com/rook/rook/blob/v1.20.10/deploy/charts/rook-ceph/values.yaml
- https://github.com/ceph/ceph-csi-operator/tree/v1.0.4/deploy/charts/ceph-csi-drivers

## Live baseline and version verification

Read-only survey at Nov 8, 2026, approximately 12:42 AM EST:
- Two deployed releases: `rook-ceph` and `rook-ceph-cluster`, both 1.20.10;
  no `ceph-csi-drivers` Helm release.
- Both full-name Drivers and `ceph-csi-operator-config` already carry Helm
  metadata naming `ceph-csi-drivers`. Their managedFields manager is `rook`; they have no owner references. This is
  not proof that a Helm release exists; the actual release list is checked too.
- CSI manager Available 1/1; RBD and CephFS controllers Available 2/2; node
  DaemonSets ready 5/5. Workloads still use the four temporary short-name SAs.
- Saved source specs include controller resource settings, storage affinity,
  GPU tolerations, replicas 2, hostNetwork in defaults, priority classes,
  image-set reference, node update strategy, and kubelet path.
- The earlier handoff said Ceph 20.2.4, but this read-only survey instead found
  CephCluster `spec.cephVersion.image` **and all mon/mgr/OSD Deployment container
  images** set to `quay.io/ceph/ceph:v19.2.7`, matching this checkout's pin.
  Do not override that pin based on the stale handoff. Verify runtime health and
  versions before applying; the CSI migration must not also change the data plane.
  The preflight refuses a requested pin differing from the live CephCluster tag.

The role writes timestamped exports (Helm release metadata, CSI resources,
RBAC, workload status; no Secrets) to `/root/rook-csi-export/` on its target
controller **before** any operator upgrade or ownership patch.

## Settings and ownership safety

The role renders baseline values, merges saved live Driver/OperatorConfig specs,
then renders the actual pinned chart and compares it back to the live specs.
Explicit existing settings win over baseline defaults. Unknown settings the chart
cannot represent fail closed instead of being silently dropped. Missing/default
settings are made explicit where needed. The intentional service-account rename
is the only nonempty Driver setting excluded from preservation checks:

- Driver names stay `rook-ceph.rbd.csi.ceph.com` and
  `rook-ceph.cephfs.csi.ceph.com`; provisioner names and StorageClasses do not change.
- New SAs are `rook-ceph-rbd-csi-ceph-com-{ctrlplugin,nodeplugin}-sa` and
  `rook-ceph-cephfs-csi-ceph-com-{ctrlplugin,nodeplugin}-sa`.
- 23 chart objects: two Drivers, one OperatorConfig, and four complete sets of
  SA/Role/RoleBinding/ClusterRole/ClusterRoleBinding.
- NFS and NVMe-oF are explicitly disabled, and no CephConnection/ClientProfile is
  chart-managed. Rook retains ownership of monitor endpoints and client profiles.
- Snapshot policy is `volumeSnapshot` for new installs, preserving the old enabled
  RBD/CephFS snapshotters. Explicit live snapshot policy takes precedence.
- Legacy `csi.readAffinity.enabled: true` had no labels configured; the exported
  live CephConnection has no `crushLocationLabels`. Its effective state is retained,
  not replaced with invented topology labels. Enabling read affinity needs a
  separately reviewed nonempty set of actual node/CRUSH labels on the Rook-owned
  connection. The new chart must not take over that dynamically reconciled object.
- Removed old driver-placement knobs from `rook-ceph` values: the pinned 1.20 chart
  no longer exposes them. `csi.installCsiOperator` and
  `csi.createCsiOperatorResources` remain explicit; Rook still supplies CSI images.

Before changing the operator, preflight inventories **every rendered object** for
ownership collisions, including namespaced and cluster RBAC. A different Helm
release/namespace, another manager label, owner references on an unowned object,
custom SA settings, differing RBAC permissions/binding targets, or unrepresentable
specs stops the migration. Existing short-name recovery RBAC is never adopted,
changed or deleted. No `--take-ownership`, blanket deletes or recreate strategy.

`rook_csi_adopt_existing` defaults to `false`. If otherwise-compatible chart
objects lack Helm metadata, failure prints their exact label/annotation commands
and export location. Review the exports first. Opt-in patches only standard Helm
metadata, using resourceVersion to fail on concurrent changes. It never steals
foreign-owned objects or bypasses settings/RBAC checks. Already correctly stamped
objects (as in this survey) do not require the flag. Check mode only previews
adoption; it never patches the cluster.

## Invocations for BrianM (not executed)

Wait for the current Kubernetes upgrade to finish. Verify Ceph and CSI health and
confirm the actual CephCluster image first:

```sh
helm list -n rook-ceph
kubectl -n rook-ceph get cephclusters.ceph.rook.io -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.cephVersion.image}{"\n"}{end}'
kubectl -n rook-ceph get drivers.csi.ceph.io,operatorconfigs.csi.ceph.io,cephconnections.csi.ceph.io -o yaml --show-managed-fields=true
kubectl -n rook-ceph get deploy,ds,sa
kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph -s
```

In the normal container venv, with access to the Ansible inventory/vaults:

```sh
# Existing cluster, after confirming live Ceph still matches the checked-in pin:
ansible-playbook -i hosts -i vaults run.yaml --tags rook --check --diff
# Review diff/exports, then apply in a maintenance window:
ansible-playbook -i hosts -i vaults run.yaml --tags rook
```

Only if preflight identifies **unowned, otherwise compatible** objects and the
exports have been reviewed, add `-e rook_csi_adopt_existing=true` to both commands.
Use the same commands for a fresh cluster with the intended Ceph pin.
Prerequisites: controller Helm/kubectl, Python 3 with PyYAML (the role
ensures `python3-yaml` in apply mode), kubernetes.core collection/dependencies,
working cluster-admin access, inventory/vaults, healthy existing Ceph. A brand-new
controller needs these prerequisites before a complete check-mode render; check
mode cannot install missing Python packages or create missing CRDs. The role's
planner can render without CSI CRDs, but Helm's later dry run may still need them.

Post-install checks require all four chart SAs, Available/fully updated controller
Deployments and fully ready/updated node DaemonSets. The same read-only validation
runs cluster-wide in Kubernetes upgrade prerequisites **before any drain**. It
intentionally rejects the temporary short-account-only repair; install the durable
chart first. Post-install waits up to ten minutes; pre-drain fails immediately.
Check mode skips the post-install gate because resources have not been installed.

After a successful rollout, test a new PVC and mount/unmount for each provisioner,
verify existing workloads, and recheck Ceph health. Only then consider removing
short-name manual RBAC as a **separate reviewed manual operation**. First inspect
all Deployments/DaemonSets, running and terminating pods, Jobs and bindings for
references. Never remove the repair while old pods still use its accounts.

## Offline tests and limits

`tests/test_rook_csi.py` exercises the **actual unpacked 1.0.4 chart** for fresh,
simulated supported-1.19 migration, exported repaired-current specs, and identical
rerenders. It checks all 23 objects, ownership refusal/adoption planning, unknown
settings, differing RBAC, supported version gates and partial readiness failures.
The fixture is nonsecret and anonymized. Set `HELM` and `CSI_CHART` when running:

```sh
HELM=/path/to/helm CSI_CHART=/path/to/ceph-csi-drivers python3 -m unittest discover -s tests -p test_rook_csi.py -v
```

These prove rendering and guard behavior, not a running migration. No live Helm
installation, adoption, controller rollout, PVC operation, playbook or Ansible
syntax-check execution was performed. Readiness/ownership must be rechecked at
apply time; current readiness can change while the node upgrade is running.
