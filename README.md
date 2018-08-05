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
5. Modify `/etc/nova/nova-compute.conf`
  * `compute_driver=bampi.BampiDriver`
  * `sync_power_state_interval=60`
6. Restart nova-compute service


Reference for BAMPI Driver Operation Variables
---------------------------------------------

### BAMPI Basic Information

```
BAMPI_IP_ADDR = '<IP_ADDR>'
BAMPI_PORT = <PORT>
BAMPI_API_BASE_URL = '<URL>'
BAMPI_USER = '<USERNAME>'
BAMPI_PASS = '<PASSWORD>'
DUMMY_IMG_NAME = 'IMG_NAME'
```

### Peregrine Basic Information

```
PEREGRINE_IP_ADDR = '<IP_ADDR>'
PEREGRINE_PORT = <PORT>
PEREGRINE_API_BASE_URL = '<URL>'
PEREGRINE_USER = '<USERNAME'
PEREGRINE_PASS = '<PASSWORD>'
```

### HaaS-core Basic Information

```
HAAS_CORE_IP_ADDR = '<IP_ADDR>'
HAAS_CORE_PORT = <PORT>
HAAS_CORE_API_BASE_URL = '<URL>'
OS_USER = '<USERNAME>'
OS_PASS = '<PASSWORD>'
```

### Network Configuration for Provisioning

```
PROVISION_VLAN_ID = <VLAN_ID>
```
