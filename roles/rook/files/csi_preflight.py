#!/usr/bin/env python3
"""Read-only cluster preflight; export sources and render the pinned Helm release.

Never applies resources. Ansible handles explicitly approved metadata adoption.
"""
import argparse
import copy
import datetime
import json
from pathlib import Path
import re
import subprocess
import sys

import yaml

RELEASE = 'ceph-csi-drivers'
REPOSITORY = 'https://ceph.github.io/ceph-csi-operator'
CUSTOM = {
    'Driver': 'drivers.csi.ceph.io',
    'OperatorConfig': 'operatorconfigs.csi.ceph.io',
    'CephConnection': 'cephconnections.csi.ceph.io',
    'CephCluster': 'cephclusters.ceph.rook.io',
}
PLUGINS = ('nodePlugin', 'controllerPlugin')
# Scheduling the template owns outright: it replaces live values instead of being merged
# under them, so a toleration fix in ceph-csi-drivers.yaml.j2 reaches the cluster.
TEMPLATE_OWNED = ('tolerations',)


def load(text):
    # kubectl --ignore-not-found prints nothing when no objects match.
    return json.loads(text) if text.strip() else {'kind': 'List', 'items': []}


def run(argv):
    result = subprocess.run(argv, text=True, capture_output=True, timeout=120)
    if result.returncode:
        raise RuntimeError('Command failed: ' + ' '.join(argv) + '\n' + result.stderr)
    return result.stdout


def items(data):
    return data.get('items', []) if data.get('kind', '').endswith('List') else [data]


def merge(base, override):
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def preserved(old, new, path=()):
    """Reject unrepresentable live settings; permit only the intentional SA rename.

    Absent/empty old fields defer to operator/chart defaults. New explicit fields
    are allowed, but nonempty existing fields must retain their values.
    """
    if path in [('nodePlugin', 'serviceAccountName'),
                ('controllerPlugin', 'serviceAccountName')]:
        return []
    if len(path) >= 2 and path[-2] in PLUGINS and path[-1] in TEMPLATE_OWNED:
        return []
    if old is None or old == '' or old == {}:
        return []
    if isinstance(old, dict):
        if not isinstance(new, dict):
            return ['.'.join(path)]
        return [error for key, value in old.items()
                for error in preserved(value, new.get(key), path + (key,))]
    return [] if old == new else ['.'.join(path)]


def owner_state(obj, namespace):
    meta = obj['metadata']
    annotations = meta.get('annotations', {})
    labels = meta.get('labels', {})
    release = annotations.get('meta.helm.sh/release-name')
    release_ns = annotations.get('meta.helm.sh/release-namespace')
    manager = labels.get('app.kubernetes.io/managed-by')
    if meta.get('ownerReferences'):
        return 'foreign'
    if release == RELEASE and release_ns == namespace and manager == 'Helm':
        return 'owned'
    if (release not in (None, RELEASE) or release_ns not in (None, namespace)
            or manager not in (None, 'Helm') or meta.get('ownerReferences')):
        return 'foreign'
    return 'unowned'


def build_values(base, live):
    result = copy.deepcopy(base)
    configs = [o for o in live if o['kind'] == 'OperatorConfig']
    if any(o['metadata']['name'] != base['operatorConfig']['name'] for o in configs):
        raise ValueError('Unexpected OperatorConfig; reconcile its scope manually before migration.')
    for obj in configs:
        if set(obj['spec']) - {'driverSpecDefaults'}:
            raise ValueError('OperatorConfig contains fields unsupported by this chart values mapping.')
        result['operatorConfig']['driverSpecDefaults'] = merge(
            result['operatorConfig']['driverSpecDefaults'], obj['spec'].get('driverSpecDefaults', {}))
    for key in ('rbd', 'cephfs'):
        result['drivers'][key] = merge(result['drivers'][key],
                                       result['operatorConfig']['driverSpecDefaults'])
    expected = {result['drivers'][key]['name']: key for key in ('rbd', 'cephfs')}
    for obj in live:
        if obj['kind'] != 'Driver':
            continue
        name = obj['metadata']['name']
        if name not in expected:
            raise ValueError('Unexpected Driver ' + name + '; migrate explicitly instead of ignoring it.')
        key = expected[name]
        spec = copy.deepcopy(obj['spec'])
        for plugin in ('nodePlugin', 'controllerPlugin'):
            spec.get(plugin, {}).pop('serviceAccountName', None)
            if spec.get(plugin, {}).get('imagePullPolicy') == '':
                spec[plugin].pop('imagePullPolicy')
        result['drivers'][key] = merge(result['drivers'][key], spec)
    template_owned(result, base)
    return result


def template_owned(result, base):
    """Put the template's TEMPLATE_OWNED plugin fields back over merged live values."""
    specs = [(result['operatorConfig']['driverSpecDefaults'],
              base['operatorConfig']['driverSpecDefaults'])]
    specs += [(result['drivers'][key], base['drivers'][key]) for key in ('rbd', 'cephfs')]
    for target, source in specs:
        for plugin in PLUGINS:
            for field in TEMPLATE_OWNED:
                if field in source.get(plugin, {}):
                    target.setdefault(plugin, {})[field] = copy.deepcopy(source[plugin][field])


def validate_existing(existing, rendered, namespace):
    desired = {(o['kind'], o['metadata']['name']): o for o in rendered}
    adoption = []
    defaults = next((o['spec'].get('driverSpecDefaults', {}) for o in rendered
                     if o['kind'] == 'OperatorConfig'), {})
    for obj in existing:
        key = obj['kind'], obj['metadata']['name']
        target = desired.get(key)
        if target is None:
            continue
        state = owner_state(obj, namespace)
        if state == 'foreign':
            raise ValueError('Foreign ownership on %s/%s; no automatic takeover is allowed.' % key)
        if obj['kind'] in ('Driver', 'OperatorConfig'):
            target_spec = merge(defaults, target['spec']) if obj['kind'] == 'Driver' else target['spec']
            errors = preserved(obj['spec'], target_spec)
            if errors:
                raise ValueError('%s/%s would lose/change settings: %s' % (*key, ', '.join(errors)))
        # Never replace extra permissions or immutable binding targets on collision.
        if obj['kind'] in ('Role', 'ClusterRole', 'RoleBinding', 'ClusterRoleBinding'):
            for field in ('rules', 'subjects', 'roleRef', 'aggregationRule'):
                if obj.get(field) != target.get(field):
                    raise ValueError('%s/%s differs in %s; reconcile RBAC manually, not by takeover.' % (*key, field))
        if obj['kind'] == 'ServiceAccount':
            for field in ('imagePullSecrets', 'automountServiceAccountToken'):
                if field in obj and obj[field] != target.get(field):
                    raise ValueError('%s/%s custom %s requires manual preservation.' % (*key, field))
        if state == 'unowned':
            adoption.append({
                'apiVersion': obj['apiVersion'], 'kind': obj['kind'],
                'metadata': {'name': key[1],
                             **({'resourceVersion': meta_version} if (meta_version := obj['metadata'].get('resourceVersion')) else {}),
                             **({'namespace': namespace} if obj['metadata'].get('namespace') else {}),
                             'labels': {'app.kubernetes.io/managed-by': 'Helm'},
                             'annotations': {'meta.helm.sh/release-name': RELEASE,
                                             'meta.helm.sh/release-namespace': namespace}},
            })
    return adoption


def validate_versions(releases, target):
    if target.lstrip('v') != '1.20.10':
        raise ValueError('This migration is verified for Rook 1.20.10 only; review chart compatibility before repinning.')
    for release in releases:
        if release['name'] == 'rook-ceph':
            match = re.search(r'(\d+)\.(\d+)\.(\d+)$', release['chart'])
            if not match:
                raise ValueError('Cannot determine installed Rook chart version.')
            current = tuple(map(int, match.groups()))
            if not (1, 19, 5) <= current <= (1, 20, 10):
                raise ValueError('Stage Rook through 1.19.5+ first; no minor skipping or downgrades.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--values', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--export-root', required=True)
    parser.add_argument('--rook-version', required=True)
    parser.add_argument('--chart-version', required=True)
    parser.add_argument('--ceph-version', required=True)
    parser.add_argument('--kubeconfig', default='/etc/kubernetes/admin.conf')
    args = parser.parse_args()
    if args.chart_version != '1.0.4':
        raise ValueError('Only drivers chart 1.0.4 was verified with Rook 1.20.10.')
    ns = args.namespace
    kube = ['kubectl', '--kubeconfig', args.kubeconfig, '-n', ns]
    export = Path(args.export_root) / datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    export.mkdir(mode=0o700, parents=True)
    releases = json.loads(run(['helm', 'list', '--kubeconfig', args.kubeconfig, '-n', ns, '-o', 'json']))
    (export / 'helm.json').write_text(json.dumps(releases, indent=2))
    validate_versions(releases, args.rook_version)
    crds = items(load(run(kube + ['get', 'crd', *CUSTOM.values(), '--ignore-not-found', '-o', 'json'])))
    available = {o['metadata']['name'] for o in crds}
    live = []
    for resource in CUSTOM.values():
        if resource in available:
            live.extend(items(json.loads(run(kube + ['get', resource, '-o', 'json', '--show-managed-fields=true']))))
    (export / 'source-resources.json').write_text(json.dumps(live, indent=2))
    for obj in live:
        if obj['kind'] == 'CephCluster':
            image = obj.get('spec', {}).get('cephVersion', {}).get('image', '')
            tag = image.rsplit(':', 1)[-1].lstrip('v')
            if tag != args.ceph_version.lstrip('v'):
                raise ValueError('Ceph image pin differs from live %s. Keep the current Ceph version for this CSI migration; do not combine with a data-plane change.' % image)
    base = yaml.safe_load(Path(args.values).read_text())
    values = build_values(base, live)
    Path(args.output).write_text(yaml.safe_dump(values, sort_keys=False))
    rendered_text = run(['helm', 'template', RELEASE, 'ceph-csi-drivers', '--repo', REPOSITORY,
                         '--version', args.chart_version, '-n', ns, '-f', args.output])
    (export / 'rendered.yaml').write_text(rendered_text)
    (export / 'values.yaml').write_text(yaml.safe_dump(values, sort_keys=False))
    rendered = [o for o in yaml.safe_load_all(rendered_text) if o]
    existing = []
    grouped = {}
    for obj in rendered:
        kind = obj['kind']
        if kind in CUSTOM and CUSTOM[kind] not in available:
            continue
        grouped.setdefault(kind, []).append(obj['metadata']['name'])
    for kind, names in grouped.items():
        existing.extend(items(load(run(kube + ['get', kind, *names, '--ignore-not-found', '-o', 'json', '--show-managed-fields=true']))))
    (export / 'colliding-resources.json').write_text(json.dumps(existing, indent=2))
    adoption = validate_existing(existing, rendered, ns)
    commands = []
    for obj in adoption:
        meta = obj['metadata']
        prefix = ['kubectl', '--kubeconfig', args.kubeconfig]
        if meta.get('namespace'):
            prefix += ['-n', ns]
        ref = [obj['kind'], meta['name']]
        commands.append(' '.join(prefix + ['label'] + ref + ['app.kubernetes.io/managed-by=Helm', '--overwrite']))
        commands.append(' '.join(prefix + ['annotate'] + ref + ['meta.helm.sh/release-name=' + RELEASE,
                        'meta.helm.sh/release-namespace=' + ns, '--overwrite']))
    print(json.dumps({'export_dir': str(export), 'values_file': args.output,
                      'adoption': adoption, 'adopt_commands': commands}))


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
