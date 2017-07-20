# OpenStack Nova Compute driver for ITRI HaaS

* Platform: Ubuntu 14.04 LTS

## Installation Guide

1. Download ITRI driver package.zip
2. Unzip the package.zip
3. Modify fake.py 
  * `BAMPI_IP_ADDR = '127.0.0.1'`
  * `PEREGRINE_IP_ADDR = '127.0.0.1'`
  * `HAAS_CORE_IP_ADDR = '127.0.0.1'`
  * `PROVISION_VLAN_ID = x`
4. Copy fake.py to `/usr/lib/python2.7/dist-packages/nova/virt/`
5. Modify `/etc/nova/nova-compute.conf` 
  * `compute_driver=fake.FakeDriver`
  * `sync_power_state_interval=60`
6. Restart nova-compute service

