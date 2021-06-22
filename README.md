# Bare Metal Kubernetes Cluster

This repository contains the needed elements to setup a bare metal repository.

NOTE: This is not a production ready cluster as it does not configure multiple masters, nor does it configure an etcd cluster.

This repo does basic configuration for the following components:

- **[Ansible](https://www.ansible.com/)** - for automation of deployment
- **[Kubernetes](https://kubernetes.io/)**
- **[Calico](https://www.projectcalico.org/)** - for networking
- **[MetalLB](https://metallb.universe.tf/)** - for load balancing
- **[Rook](https://rook.io/)/[Ceph](https://ceph.io/)** - for automatic provisioning of storage
- **[Helm](https://helm.sh/)** - for software management and installation
- **Kubernetes [metrics-server](https://github.com/kubernetes-sigs/metrics-server)** - for basic resource monitoring
- **[Prometheus](https://prometheus.io/)/[Grafana](https://grafana.com/)** - for advanced resource monitoring and visualization

## The Scripts

    Naming conventions - if a script starts with:
    - init - initialization scripts needed to configure base systems and for use with Ansible.
    - k8s-deploy - These scripts are for deploying Kubernetes workloads
    - k8s- Kubernetes related component.

 - **init-ansible-workstation.yml** - Setup the localhost with all the needed software to work with this repository.
 - **init-server.yml** - Setup each remote server with basic settings like timezone, hostname, sudo etc.
 - **install-utils.yml** - Install helpful utilities that don't fall into other categories.
 - **k8s-setup.yml** - Install and configure all needed components for running Kubernetes.