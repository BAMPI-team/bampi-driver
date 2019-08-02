BAMPI Driver for OpenStack Nova Compute
=======================================

* Target platform: OpenStack Newton on Ubuntu 16.04 LTS
* Test platform: OpenStack Newton on Ubuntu 16.04 LTS

Installation Guide
------------------

1. Download BAMPI driver release package
2. Unzip the package
3. Modify operation variables in `bampi/driver.py`
4. Copy `bampi` directory to `/usr/lib/python2.7/dist-packages/nova/virt/`
5. Copy `bampi.py` file to `/usr/lib/python2.7/dist-packages/nova/conf/`
5. Add two lines in `/usr/lib/python2.7/dist-packages/nova/conf/__init__.py`
  * `from nova.conf import bampi`
  * `bampi.register_opts(CONF)`
5. Modify `/etc/nova/nova-compute.conf` in `[DEFAULT]` section
  * `compute_driver=bampi.BampiDriver`
  * `sync_power_state_interval=60`
6. Restart nova-compute service


Reference for BAMPI Driver Operation Variables
---------------------------------------------

### BAMPI Basic Information

For example, in `nova-compute.conf`:

```
[bampi]
bampi_endpoint = http://bampi.bampi.net/bampi/api/kddi/v1
bampi_image_endpoint = http://bampi.bampi.net/partimag
bampi_username = admin
bampi_password = admin
dummy_image_name = clonezilla-live-install-less_image.iso

peregrine_endpoint = http://peregrine.bampi.net:8282/controller/nb/v3
peregrine_username = admin
peregrine_password = admin
provision_vlan_id = 41

haas_core_endpoint = http://hcore-1.bampi.net:8080/haas-core/api
haas_core_username = admin
haas_core_password = password

backup_directory = /tmp/snapshots
```
