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


