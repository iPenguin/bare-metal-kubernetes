# Bare Metal Kubernetes Cluster

## Description

This repository contains the needed elements to setup a bare metal cluster using ansible.

NOTE: This is not a production ready cluster as it does not configure multiple masters, nor does it configure a separate etcd cluster.

This repo does/will do basic configuration for the following components:

- **[Ansible](https://www.ansible.com/)** - for automation of deployment
- **[Kubernetes](https://kubernetes.io/)**
- **[Cilium](https://www.cilium.io/)** - for networking
- **[MetalLB](https://metallb.universe.tf/)** - for load balancing
- **[Rook](https://rook.io/)/[Ceph](https://ceph.io/)** - for automatic provisioning of storage
- **[Helm](https://helm.sh/)** - for software management and installation
- **Kubernetes [metrics-server](https://github.com/kubernetes-sigs/metrics-server)** - for basic resource monitoring
- **[Prometheus](https://prometheus.io/)/[Grafana](https://grafana.com/)** - for advanced resource monitoring and visualization
- **[FreeIPA](https://www.freeipa.org/) - For centralized Identinty Management.

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
