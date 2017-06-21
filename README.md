# CKKB driver for OpenStack Nova Compute

* Target platform: RedHat
* Test platform: Ubuntu

## Installation Guide

1. Download ITRI driver package.zip
2. Unzip the package.zip
3. Modify fake.py 
  * `BAMPI_IP_ADDR = x.x.x.x`
4. Copy fake.py to `/usr/lib/python2.7/dist-packages/nova/virt/`
5. Modify `/etc/nova/nova-compute.conf` 
  * `compute_driver=fake.FakeDriver`
  * `sync_power_state_interval=60`
6. Restart nova-compute service

