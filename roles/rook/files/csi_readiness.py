#!/usr/bin/env python3
"""Read-only Rook CSI gate shared by Rook installs and Kubernetes pre-drain checks."""
import argparse
import json
import subprocess
import sys


def evaluate(objects, namespace):
    indexed = {(obj['kind'], obj['metadata']['name']): obj for obj in objects}
    errors = []
    deployments = ['ceph-csi-controller-manager']
    daemonsets = []
    for driver in ('rbd', 'cephfs'):
        prefix = namespace + '.' + driver + '.csi.ceph.com'
        deployments.append(prefix + '-ctrlplugin')
        daemonsets.append(prefix + '-nodeplugin')
        for plugin in ('ctrlplugin', 'nodeplugin'):
            account = prefix.replace('.', '-') + '-' + plugin + '-sa'
            if ('ServiceAccount', account) not in indexed:
                errors.append('missing ServiceAccount/' + account)
            kind = 'Deployment' if plugin == 'ctrlplugin' else 'DaemonSet'
            workload = indexed.get((kind, prefix + '-' + plugin))
            if workload and workload['spec']['template']['spec'].get('serviceAccountName') != account:
                errors.append(kind + '/' + prefix + '-' + plugin + ' still uses a legacy/unexpected account')
    for name in deployments:
        obj = indexed.get(('Deployment', name))
        if not obj:
            errors.append('missing Deployment/' + name)
            continue
        status = obj.get('status', {})
        desired = obj['spec'].get('replicas', 1)
        if (desired < 1 or status.get('observedGeneration', 0) < obj['metadata'].get('generation', 1)
                or status.get('replicas', desired) != desired
                or status.get('terminatingReplicas', 0) != 0
                or status.get('updatedReplicas', 0) != desired
                or status.get('readyReplicas', 0) != desired
                or status.get('availableReplicas', 0) != desired
                or not any(c['type'] == 'Available' and c['status'] == 'True'
                           for c in status.get('conditions', []))):
            errors.append('Deployment/' + name + ' is not fully Available/updated')
    for name in daemonsets:
        obj = indexed.get(('DaemonSet', name))
        if not obj:
            errors.append('missing DaemonSet/' + name)
            continue
        status = obj.get('status', {})
        desired = status.get('desiredNumberScheduled', 0)
        if (desired < 1 or status.get('observedGeneration', 0) < obj['metadata'].get('generation', 1)
                or status.get('updatedNumberScheduled', 0) != desired
                or status.get('numberReady', 0) != desired
                or status.get('numberAvailable', 0) != desired
                or status.get('numberUnavailable', 0) != 0
                or status.get('numberMisscheduled', 0) != 0):
            errors.append('DaemonSet/' + name + ' is not fully ready/updated')
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--kubeconfig', default='/etc/kubernetes/admin.conf')
    args = parser.parse_args()
    result = subprocess.run(['kubectl', '--kubeconfig', args.kubeconfig, '-n', args.namespace,
                             'get', 'deploy,ds,sa', '-o', 'json'], text=True,
                            capture_output=True, timeout=30)
    if result.returncode:
        print(result.stderr, file=sys.stderr)
        return 1
    errors = evaluate(json.loads(result.stdout)['items'], args.namespace)
    if errors:
        print('CSI gate FAILED: ' + '; '.join(errors), file=sys.stderr)
        return 1
    print('CSI gate passed: chart accounts exist, controllers Available, node DaemonSets fully ready.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
