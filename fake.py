# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
# Copyright (c) 2010 Citrix Systems, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
A fake (in-memory) hypervisor+api.

Allows nova testing w/o a hypervisor.  This module also documents the
semantics of real hypervisor connections.

"""

import collections
import contextlib

from oslo_log import log as logging
from oslo_serialization import jsonutils
from oslo_service import loopingcall
from oslo_utils import versionutils

from nova.compute import arch
from nova.compute import hv_type
from nova.compute import power_state
from nova.compute import task_states
from nova.compute import vm_mode
import nova.conf
from nova.console import type as ctype
from nova import exception
from nova.i18n import _LI
from nova.i18n import _LW
from nova.i18n import _LE
from nova.virt import diagnostics
from nova.virt import driver
from nova.virt import hardware
from nova.virt import virtapi

import requests
from requests.auth import HTTPBasicAuth

CONF = nova.conf.CONF

LOG = logging.getLogger(__name__)


_FAKE_NODES = None


# BAMPI basic information
BAMPI_IP_ADDR = '100.73.11.39'
BAMPI_PORT = 8080
BAMPI_API_BASE_URL = '/bampi/api/kddi/v1'
BAMPI_USER = 'admin'
BAMPI_PASS = 'admin'

# Peregrine basic information
PEREGRINE_IP_ADDR = '100.73.11.32'
PEREGRINE_PORT = 8282
PEREGRINE_API_BASE_URL = '/controller/nb/v3'
PEREGRINE_USER = 'admin'
PEREGRINE_PASS = 'admin'

# HaaS-core basic information
HAAS_CORE_IP_ADDR = '100.73.11.33'
HAAS_CORE_PORT = 8080
HAAS_CORE_API_BASE_URL = '/haas-core/api'
OS_USER = 'admin'
OS_PASS = 'openstack'

DUMMY_IMG_NAME = 'install-less_image.iso'

# Network configuration for provisioning
PROVISION_VLAN_ID = 5

# Power state mapping
power_state_map = {
    'on': power_state.RUNNING,
    'off': power_state.SHUTDOWN
}


def set_nodes(nodes):
    """Sets FakeDriver's node.list.

    It has effect on the following methods:
        get_available_nodes()
        get_available_resource

    To restore the change, call restore_nodes()
    """
    global _FAKE_NODES
    _FAKE_NODES = nodes


def restore_nodes():
    """Resets FakeDriver's node list modified by set_nodes().

    Usually called from tearDown().
    """
    global _FAKE_NODES
    _FAKE_NODES = [CONF.host]


class FakeInstance(object):

    def __init__(self, name, state, uuid):
        self.name = name
        self.state = state
        self.uuid = uuid

    def __getitem__(self, key):
        return getattr(self, key)


class Resources(object):
    vcpus = 0
    memory_mb = 0
    local_gb = 0
    vcpus_used = 0
    memory_mb_used = 0
    local_gb_used = 0

    def __init__(self, vcpus=8, memory_mb=8000, local_gb=500):
        self.vcpus = vcpus
        self.memory_mb = memory_mb
        self.local_gb = local_gb

    def claim(self, vcpus=0, mem=0, disk=0):
        self.vcpus_used += vcpus
        self.memory_mb_used += mem
        self.local_gb_used += disk

    def release(self, vcpus=0, mem=0, disk=0):
        self.vcpus_used -= vcpus
        self.memory_mb_used -= mem
        self.local_gb_used -= disk

    def dump(self):
        return {
            'vcpus': self.vcpus,
            'memory_mb': self.memory_mb,
            'local_gb': self.local_gb,
            'vcpus_used': self.vcpus_used,
            'memory_mb_used': self.memory_mb_used,
            'local_gb_used': self.local_gb_used
        }


class FakeDriver(driver.ComputeDriver):
    capabilities = {
        "has_imagecache": True,
        "supports_recreate": True,
        "supports_migrate_to_same_host": True
        }

    # Since we don't have a real hypervisor, pretend we have lots of
    # disk and ram so this driver can be used to test large instances.
    vcpus = 1000
    memory_mb = 800000
    local_gb = 600000

    """Fake hypervisor driver."""

    def __init__(self, virtapi, read_only=False):
        super(FakeDriver, self).__init__(virtapi)
        self.instances = {}
        self.resources = Resources(
            vcpus=self.vcpus,
            memory_mb=self.memory_mb,
            local_gb=self.local_gb)
        self.host_status_base = {
          'hypervisor_type': 'fake',
          'hypervisor_version': versionutils.convert_version_to_int('1.0'),
          'hypervisor_hostname': CONF.host,
          'cpu_info': {},
          'disk_available_least': 0,
          'supported_instances': [(arch.X86_64, hv_type.FAKE, vm_mode.HVM)],
          'numa_topology': None,
          }
        self._mounts = {}
        self._interfaces = {}
        self.active_migrations = {}
        if not _FAKE_NODES:
            set_nodes([CONF.host])

    def init_host(self, host):
        return

    def list_instances(self):
        return [self.instances[uuid].name for uuid in self.instances.keys()]

    def list_instance_uuids(self):
        return self.instances.keys()

    def plug_vifs(self, instance, network_info):
        """Plug VIFs into networks."""
        pass

    def unplug_vifs(self, instance, network_info):
        """Unplug VIFs from networks."""
        pass

    def spawn(self, context, instance, image_meta, injected_files,
              admin_password, network_info=None, block_device_info=None):
        uuid = instance.uuid
        state = power_state.RUNNING
        flavor = instance.flavor
        self.resources.claim(
            vcpus=flavor.vcpus,
            mem=flavor.memory_mb,
            disk=flavor.root_gb)
        fake_instance = FakeInstance(instance.name, state, uuid)
        self.instances[uuid] = fake_instance

        # XXX: Where dirty hack begins
        def _get_task_status(t_id):
            r = requests.get("http://{bampi_ip_addr}:{bampi_port}{bampi_api_base_url}/tasks/{task_id}"
                                .format(bampi_ip_addr=BAMPI_IP_ADDR,
                                        bampi_port=BAMPI_PORT,
                                        bampi_api_base_url=BAMPI_API_BASE_URL,
                                        task_id=t_id),
                             auth=HTTPBasicAuth(BAMPI_USER, BAMPI_PASS))
            t_status = r.json()['status']
            return t_status

        def _wait_for_ready():
            """Called at an interval until the task is successfully ended."""
            status = _get_task_status(t_id)
            LOG.info(_LI("[BAMPI] Task %(task_id)s status=%(status)s"),
                     {'task_id': t_id,
                      'status': status},
                     instance=instance)

            if status == 'Success':
                LOG.info(_LI("[BAMPI] Task %(task_id)s ended successfully."),
                         {'task_id': t_id,
                          'status': status},
                         instance=instance)
                raise loopingcall.LoopingCallDone()
            if status == 'Error':
                LOG.error(_LE("[BAMPI] Task %(task_id)s failed."),
                          {'task_id': t_id},
                          instance=instance)
                raise loopingcall.LoopingCallDone(False)

        # Specify hostname to decide which bare metal server to provision
        LOG.info(_LI("[BAMPI] hostname=%s"), instance.hostname, instance=instance)
        r = requests.get("http://{bampi_ip_addr}:{bampi_port}{bampi_api_base_url}/servers"
                            .format(bampi_ip_addr=BAMPI_IP_ADDR,
                                    bampi_port=BAMPI_PORT,
                                    bampi_api_base_url=BAMPI_API_BASE_URL),
                         auth=HTTPBasicAuth(BAMPI_USER, BAMPI_PASS))
        servers = r.json()
        try:
            server = (s for s in servers if s['hostname'].lower() == instance.hostname).next()
        except StopIteration:
            LOG.warn(_LW("[BAMPI] Bare-metal server '%s' specified not found, fallback to fake instance"),
                     instance.hostname,
                     instance=instance)
            # TODO: Raise some exception to upper layer
            return

        # Check dummy or not
        if image_meta.name == DUMMY_IMG_NAME:
            LOG.info(_LI("[BAMPI] Dummy RestoreOS on hostname=%s"), instance.hostname, instance=instance)
            try:
                r = requests.get("http://{bampi_ip_addr}:{bampi_port}{bampi_api_base_url}/servers/{hostname}/powerStatus"
                                    .format(bampi_ip_addr=BAMPI_IP_ADDR,
                                            bampi_port=BAMPI_PORT,
                                            bampi_api_base_url=BAMPI_API_BASE_URL,
                                            hostname=instance.hostname),
                                 auth=HTTPBasicAuth(BAMPI_USER, BAMPI_PASS))
                r.raise_for_status()
            except requests.exceptions.HTTPError as e:
                LOG.warn(_LW("[BAMPI] %s" % e), instance=instance)
                raise exception.InstanceNotFound(instance_id=instance.uuid)
            else:
                # Dummy RestoreOS, do nothing
                default_tasks_q = [
                    {
                        'taskType': 'change_boot_mode',
                        'taskProfile': 'Disabled'
                    }
                ]
        else:
            # Normal provisioning workflow
            default_tasks_q = [
                {
                    'taskType': 'change_boot_mode',
                    'taskProfile': 'PXE'
                },
                {
                    'taskType': 'restore_os',
                    'taskProfile': image_meta.name
                },
                {
                    'taskType': 'change_boot_mode',
                    'taskProfile': 'Disabled'
                }
            ]

        # Iterate through all tasks in default task queue
        for task in default_tasks_q:
            task['hostname'] = server['hostname']

            # Requesting outer service to execute the task
            LOG.info(_LI("[BAMPI] REQ => Starting provision task %s..."),
                     task['taskType'], instance=instance)
            r = requests.post('http://{bampi_ip_addr}:{bampi_port}{bampi_api_base_url}/tasks'
                                .format(bampi_ip_addr=BAMPI_IP_ADDR,
                                        bampi_port=BAMPI_PORT,
                                        bampi_api_base_url=BAMPI_API_BASE_URL),
                              auth=HTTPBasicAuth(BAMPI_USER, BAMPI_PASS), json=task)
            try:
                t_id = r.json()['id']
            except KeyError:
                # If we cannot find 'id' in return json...
                LOG.error(_LE("[BAMPI] RESP (provision) => task_type=%s, task_profile=%s, ret_code=%s"),
                          task['taskType'], task['taskProfile'], r.status_code,
                          instance=instance)
                raise exception.NovaException("BAMPI cannot execute task. Abort instance spawning...")
            else:
                LOG.info(_LI("[BAMPI] RESP (provision) => ret_code=%s, task_id=%s"),
                         r.status_code, t_id, instance=instance)

            # Polling for task status
            time = loopingcall.FixedIntervalLoopingCall(_wait_for_ready)
            ret = time.start(interval=5).wait()

            # Task failed, abort spawning
            if ret == False:
                raise exception.NovaException("Provision task failed. Abort instance spawning...")
            else:
                LOG.info(_LI("[BAMPI] Provision task %s:%s has ended successfully."),
                         task['taskType'],
                         task['taskProfile'],
                         instance=instance)

        LOG.info(_LI("[BAMPI] All provision tasks have ended successfully."),
                 instance=instance)

        # Get segmentation ID of desired tenant network from HaaS-core
        network_provision_payload = {}
        ni = jsonutils.loads(network_info.json())
        tnid = ni[0]['network']['id']
        LOG.info(_LI("tenant network id = %s"), tnid, instance=instance)
        LOG.info(_LI("[HAAS_CORE] REQ => network_detail..."), instance=instance)
        r = requests.get("http://{haas_core_ip_addr}:{haas_core_port}{haas_core_api_base_url}/network/get/{tenant_network_id}"
                            .format(haas_core_ip_addr=HAAS_CORE_IP_ADDR,
                                    haas_core_port=HAAS_CORE_PORT,
                                    haas_core_api_base_url=HAAS_CORE_API_BASE_URL,
                                    tenant_network_id=tnid),
                         auth=HTTPBasicAuth(OS_USER, OS_PASS))
        if r.status_code == 200:
            data = r.json()['data']
            nd = jsonutils.loads(data)
            segmentation_id = nd['provider:segmentation_id']
            LOG.info(_LI("segmentation id = %s"),
                     segmentation_id, instance=instance)
            network_provision_payload = {
                'provision_vlan': {
                    'admin_server_name': instance.display_name,
                    'port_group_name': 'PG-1',
                    'untagged_vlan': segmentation_id,
                    'tagged_vlans': []
                }
            }
        else:
            LOG.error(_LE("[HAAS_CORE] ret_code=%s"),
                      r.status_code,
                      instance=instance)
            raise exception.NovaException("Cannot get segmentation_id from HaaS-core. Abort instance spawning...")

        # Change VLAN ID from provision network to tenant network
        LOG.info(_LI("[PEREGRINE] REQ => networkProvision..."),
                 instance=instance)
        r = requests.post("http://{peregrine_ip_addr}:{peregrine_port}{peregrine_api_base_url}/networkprovision/setVlan"
                            .format(peregrine_ip_addr=PEREGRINE_IP_ADDR,
                                    peregrine_port=PEREGRINE_PORT,
                                    peregrine_api_base_url=PEREGRINE_API_BASE_URL),
                          auth=HTTPBasicAuth(PEREGRINE_USER, PEREGRINE_PASS),
                          json=network_provision_payload)
        if r.status_code == 200:
            LOG.info(_LI("[PEREGRINE] Tenant network set successfully."),
                     instance=instance)
        else:
            LOG.error(_LE("[PEREGRINE] ret_code=%s"),
                      r.status_code,
                      instance=instance)
            raise exception.NovaException("Cannot set tenant VLAN using Peregrine-H. Abort instance spawning...")

    def snapshot(self, context, instance, image_id, update_task_state):
        if instance.uuid not in self.instances:
            raise exception.InstanceNotRunning(instance_id=instance.uuid)
        update_task_state(task_state=task_states.IMAGE_UPLOADING)

    def reboot(self, context, instance, network_info, reboot_type,
               block_device_info=None, bad_volumes_callback=None):
        LOG.info(_LI("Reboot %(hostname)s, reboot_type=%(reboot_type)s"),
                 {'hostname': instance.hostname, 'reboot_type': reboot_type},
                 instance=instance)
        payload = {'status': 'reset'}
        if reboot_type == 'SOFT':
            payload['status'] = 'cycle'

        LOG.info(_LI("[BAMPI] Power %(reboot_type)s hostname=%(hostname)s"),
                 {'hostname': instance.hostname, 'reboot_type': reboot_type},
                 instance=instance)
        try:
            r = requests.put("http://{bampi_ip_addr}:{bampi_port}{bampi_api_base_url}/servers/{hostname}/powerStatus"
                                .format(bampi_ip_addr=BAMPI_IP_ADDR,
                                        bampi_port=BAMPI_PORT,
                                        bampi_api_base_url=BAMPI_API_BASE_URL,
                                        hostname=instance.hostname),
                             auth=HTTPBasicAuth(BAMPI_USER, BAMPI_PASS),
                             json=payload)
            r.raise_for_status()
        except requests.exception.HTTPError as e:
            LOG.warn(_LW("[BAMPI] %s" % e), instance=instance)
        finally:
            self.instances[instance.uuid].state = power_state.RUNNING

    def get_host_ip_addr(self):
        return '192.168.0.1'

    def set_admin_password(self, instance, new_pass):
        pass

    def inject_file(self, instance, b64_path, b64_contents):
        pass

    def resume_state_on_host_boot(self, context, instance, network_info,
                                  block_device_info=None):
        pass

    def rescue(self, context, instance, network_info, image_meta,
               rescue_password):
        pass

    def unrescue(self, instance, network_info):
        pass

    def poll_rebooting_instances(self, timeout, instances):
        pass

    def migrate_disk_and_power_off(self, context, instance, dest,
                                   flavor, network_info,
                                   block_device_info=None,
                                   timeout=0, retry_interval=0):
        pass

    def finish_revert_migration(self, context, instance, network_info,
                                block_device_info=None, power_on=True):
        pass

    def post_live_migration_at_destination(self, context, instance,
                                           network_info,
                                           block_migration=False,
                                           block_device_info=None):
        pass

    def power_off(self, instance, timeout=0, retry_interval=0):
        LOG.info(_LI("[BAMPI] Power off hostname=%s" % instance.hostname),
                 instance=instance)

	# NOTE: Prevent machines being powered off by power status check
        # mechanism
	if instance.vm_state == 'stopped' and instance.power_state == power_state.RUNNING:
            LOG.warn(_LW("Forcing machine %s to be powered off is not allowed" % instance.hostname),
                     instance=instance)
	else:
            try:
                r = requests.put("http://{bampi_ip_addr}:{bampi_port}{bampi_api_base_url}/servers/{hostname}/powerStatus"
                                    .format(bampi_ip_addr=BAMPI_IP_ADDR,
                                            bampi_port=BAMPI_PORT,
                                            bampi_api_base_url=BAMPI_API_BASE_URL,
                                            hostname=instance.hostname),
                                 auth=HTTPBasicAuth(BAMPI_USER, BAMPI_PASS),
                                 json={'status': 'off'})
                r.raise_for_status()
            except requests.exception.HTTPError as e:
                LOG.warn(_LW("[BAMPI] %s" % e), instance=instance)
            finally:
                self.instances[instance.uuid].state = power_state.SHUTDOWN

    def power_on(self, context, instance, network_info,
                 block_device_info=None):
        LOG.info(_LI("[BAMPI] Power on hostname=%s" % instance.hostname), instance=instance)
        try:
            r = requests.put("http://{bampi_ip_addr}:{bampi_port}{bampi_api_base_url}/servers/{hostname}/powerStatus"
                                .format(bampi_ip_addr=BAMPI_IP_ADDR,
                                        bampi_port=BAMPI_PORT,
                                        bampi_api_base_url=BAMPI_API_BASE_URL,
                                        hostname=instance.hostname),
                             auth=HTTPBasicAuth(BAMPI_USER, BAMPI_PASS),
                             json={'status': 'on'})
            r.raise_for_status()
        except requests.exceptions.HTTPError as e:
            LOG.warn(_LW("[BAMPI] %s" % e), instance=instance)
        finally:
            self.instances[instance.uuid].state = power_state.RUNNING

    def trigger_crash_dump(self, instance):
        pass

    def soft_delete(self, instance):
        pass

    def restore(self, instance):
        pass

    def pause(self, instance):
        pass

    def unpause(self, instance):
        pass

    def suspend(self, context, instance):
        pass

    def resume(self, context, instance, network_info, block_device_info=None):
        pass

    def destroy(self, context, instance, network_info, block_device_info=None,
                destroy_disks=True, migrate_data=None):

        def _get_task_status(t_id):
            r = requests.get("http://{bampi_ip_addr}:{bampi_port}""{bampi_api_base_url}/tasks/{task_id}"
                                .format(bampi_ip_addr=BAMPI_IP_ADDR,
                                        bampi_port=BAMPI_PORT,
                                        bampi_api_base_url=BAMPI_API_BASE_URL,
                                        task_id=t_id),
                             auth=HTTPBasicAuth(BAMPI_USER, BAMPI_PASS))
            t_status = r.json()['status']
            return t_status

        def _wait_for_ready():
            """Called at an interval until the task is successfully ended."""
            status = _get_task_status(t_id)
            LOG.info(_LI("[BAMPI] Task %(task_id)s status=%(status)s"),
                     {'task_id': t_id,
                      'status': status},
                     instance=instance)

            if status == 'Success':
                LOG.info(_LI("[BAMPI] Task %(task_id)s ended successfully."),
                         {'task_id': t_id,
                          'status': status},
                         instance=instance)
                raise loopingcall.LoopingCallDone()
            if status == 'Error':
                LOG.error(_LE("[BAMPI] Task %(task_id)s failed."),
                          {'task_id': t_id},
                          instance=instance)
                raise loopingcall.LoopingCallDone(False)

        key = instance.uuid
        if key in self.instances:
            # Switch back to provision network
            network_provision_payload = {
                'provision_vlan': {
                    'admin_server_name': instance.display_name,
                    'port_group_name': 'PG-1',
                    'untagged_vlan': PROVISION_VLAN_ID,
                    'tagged_vlans': []
                }
            }
            # Change VLAN ID from provision network to tenant network
            LOG.info(_LI("[PEREGRINE] REQ => networkProvision..."),
                     instance=instance)
            r = requests.post("http://{peregrine_ip_addr}:{peregrine_port}{peregrine_api_base_url}/networkprovision/setVlan"
                                .format(peregrine_ip_addr=PEREGRINE_IP_ADDR,
                                        peregrine_port=PEREGRINE_PORT,
                                        peregrine_api_base_url=PEREGRINE_API_BASE_URL),
                              auth=HTTPBasicAuth(PEREGRINE_USER, PEREGRINE_PASS),
                              json=network_provision_payload)
            if r.status_code == 200:
                LOG.info(_LI("[PEREGRINE] Provision network set successfully."),
                         instance=instance)
            else:
                LOG.error(_LE("[PEREGRINE] ret_code=%s"),
                          r.status_code,
                          instance=instance)
                raise exception.NovaException("Cannot set provision VLAN using Peregrine-H. Abort instance spawning...")

            # Cleanup procedure
            default_tasks_q = [
                {
                    'taskType': 'change_boot_mode',
                    'taskProfile': 'PXE'
                },
                {
                    'taskType': 'configure_raid',
                    'taskProfile': 'clean_up_conf'
                }
            ]
            for task in default_tasks_q:
                task['hostname'] = instance.display_name

                # Requesting outer service to execute the task
                LOG.info(_LI("[BAMPI] REQ => Starting cleanup task %s..."),
                         task['taskType'], instance=instance)
                r = requests.post('http://{bampi_ip_addr}:{bampi_port}{bampi_api_base_url}/tasks'
                                    .format(bampi_ip_addr=BAMPI_IP_ADDR,
                                            bampi_port=BAMPI_PORT,
                                            bampi_api_base_url=BAMPI_API_BASE_URL),
                                  auth=HTTPBasicAuth(BAMPI_USER, BAMPI_PASS), json=task)
                try:
                    t_id = r.json()['id']
                except KeyError:
                    # If we cannot find 'id' in return json...
                    LOG.error(_LE("[BAMPI] RESP (cleanup) => task_type=%s, task_profile=%s, ret_code=%s"),
                              task['taskType'], task['taskProfile'], r.status_code,
                              instance=instance)
                    return
                else:
                    LOG.info(_LI("[BAMPI] RESP (cleanup) => ret_code=%s, task_id=%s"),
                             r.status_code, t_id, instance=instance)

                # Polling for task status
                time = loopingcall.FixedIntervalLoopingCall(_wait_for_ready)
                ret = time.start(interval=5).wait()

                # Task failed, abort destroying
                if ret == False:
                    raise exception.NovaException("Cleanup task failed. Abort instance destroying...")
                else:
                    LOG.info(_LI("[BAMPI] Cleanup task %s:%s has ended successfully."),
                             task['taskType'],
                             task['taskProfile'],
                             instance=instance)

            LOG.info(_LI("[BAMPI] All cleanup tasks have ended successfully."),
                     instance=instance)
            LOG.info(_LI("[HAAS_CORE] REQ => Mark server as available..."),
                     instance=instance)
            r = requests.put('http://{haas_core_ip_addr}:{haas_core_port}{haas_core_api_base_url}/servers/{hostname}/available'
                                .format(haas_core_ip_addr=HAAS_CORE_IP_ADDR,
                                        haas_core_port=HAAS_CORE_PORT,
                                        haas_core_api_base_url=HAAS_CORE_API_BASE_URL,
                                        hostname=instance.hostname),
                             auth=HTTPBasicAuth(OS_USER, OS_PASS))

            # Boot into disposable OS to stand-by
            LOG.info(_LI("[BAMPI] Power on hostname=%s to stand-by" % instance.hostname),
                     instance=instance)
            try:
                r = requests.put("http://{bampi_ip_addr}:{bampi_port}{bampi_api_base_url}/servers/{hostname}/powerStatus"
                                    .format(bampi_ip_addr=BAMPI_IP_ADDR,
                                            bampi_port=BAMPI_PORT,
                                            bampi_api_base_url=BAMPI_API_BASE_URL,
                                            hostname=instance.hostname),
                                 auth=HTTPBasicAuth(BAMPI_USER, BAMPI_PASS),
                                 json={'status': 'on'})
                r.raise_for_status()
            except requests.exceptions.HTTPError as e:
                LOG.warn(_LW("[BAMPI] %s" % e), instance=instance)

            # Back to fake driver default flow
            flavor = instance.flavor
            self.resources.release(
                vcpus=flavor.vcpus,
                mem=flavor.memory_mb,
                disk=flavor.root_gb)
            del self.instances[key]
        else:
            LOG.warning(_LW("Key '%(key)s' not in instances '%(inst)s'"),
                        {'key': key,
                         'inst': self.instances}, instance=instance)

    def cleanup(self, context, instance, network_info, block_device_info=None,
                destroy_disks=True, migrate_data=None, destroy_vifs=True):
        pass

    def attach_volume(self, context, connection_info, instance, mountpoint,
                      disk_bus=None, device_type=None, encryption=None):
        """Attach the disk to the instance at mountpoint using info."""
        instance_name = instance.name
        if instance_name not in self._mounts:
            self._mounts[instance_name] = {}
        self._mounts[instance_name][mountpoint] = connection_info

    def detach_volume(self, connection_info, instance, mountpoint,
                      encryption=None):
        """Detach the disk attached to the instance."""
        try:
            del self._mounts[instance.name][mountpoint]
        except KeyError:
            pass

    def swap_volume(self, old_connection_info, new_connection_info,
                    instance, mountpoint, resize_to):
        """Replace the disk attached to the instance."""
        instance_name = instance.name
        if instance_name not in self._mounts:
            self._mounts[instance_name] = {}
        self._mounts[instance_name][mountpoint] = new_connection_info

    def attach_interface(self, instance, image_meta, vif):
        if vif['id'] in self._interfaces:
            raise exception.InterfaceAttachFailed(
                    instance_uuid=instance.uuid)
        self._interfaces[vif['id']] = vif

    def detach_interface(self, instance, vif):
        try:
            del self._interfaces[vif['id']]
        except KeyError:
            raise exception.InterfaceDetachFailed(
                    instance_uuid=instance.uuid)

    def get_info(self, instance):
        if instance.uuid not in self.instances:
            # Instance not found may caused by fake driver memory lost...
            try:
                r = requests.get("http://{bampi_ip_addr}:{bampi_port}{bampi_api_base_url}/servers/{hostname}/powerStatus"
                                    .format(bampi_ip_addr=BAMPI_IP_ADDR,
                                            bampi_port=BAMPI_PORT,
                                            bampi_api_base_url=BAMPI_API_BASE_URL,
                                            hostname=instance.hostname),
                                 auth=HTTPBasicAuth(BAMPI_USER, BAMPI_PASS))
                r.raise_for_status()
            except requests.exceptions.HTTPError as e:
                LOG.warn(_LW("[BAMPI] %s" % e), instance=instance)
                raise exception.InstanceNotFound(instance_id=instance.uuid)
            else:
                p_st = r.json()['status']
                state = power_state_map[p_st]

                # Construct the lost fake instance...
                fake_instance = FakeInstance(instance.name, state, instance.uuid)
                self.instances[instance.uuid] = fake_instance

                return hardware.InstanceInfo(state=state,
                                             max_mem_kb=0,
                                             mem_kb=0,
                                             num_cpu=2,
                                             cpu_time_ns=0)

        i = self.instances[instance.uuid]

        LOG.info(_LI("[BAMPI] get_info hostname=%s" % instance.hostname),
                 instance=instance)
        try:
            r = requests.get("http://{bampi_ip_addr}:{bampi_port}{bampi_api_base_url}/servers/{hostname}/powerStatus"
                                .format(bampi_ip_addr=BAMPI_IP_ADDR,
                                        bampi_port=BAMPI_PORT,
                                        bampi_api_base_url=BAMPI_API_BASE_URL,
                                        hostname=instance.hostname),
                             auth=HTTPBasicAuth(BAMPI_USER, BAMPI_PASS))
            r.raise_for_status()
        except requests.exceptions.HTTPError as e:
            LOG.warn(_LW("[BAMPI] %s" % e), instance=instance)
        else:
            p_st = r.json()['status']
            i.state = power_state_map[p_st]

        return hardware.InstanceInfo(state=i.state,
                                     max_mem_kb=0,
                                     mem_kb=0,
                                     num_cpu=2,
                                     cpu_time_ns=0)

    def get_diagnostics(self, instance):
        return {'cpu0_time': 17300000000,
                'memory': 524288,
                'vda_errors': -1,
                'vda_read': 262144,
                'vda_read_req': 112,
                'vda_write': 5778432,
                'vda_write_req': 488,
                'vnet1_rx': 2070139,
                'vnet1_rx_drop': 0,
                'vnet1_rx_errors': 0,
                'vnet1_rx_packets': 26701,
                'vnet1_tx': 140208,
                'vnet1_tx_drop': 0,
                'vnet1_tx_errors': 0,
                'vnet1_tx_packets': 662,
        }

    def get_instance_diagnostics(self, instance):
        diags = diagnostics.Diagnostics(state='running', driver='fake',
                hypervisor_os='fake-os', uptime=46664, config_drive=True)
        diags.add_cpu(time=17300000000)
        diags.add_nic(mac_address='01:23:45:67:89:ab',
                      rx_packets=26701,
                      rx_octets=2070139,
                      tx_octets=140208,
                      tx_packets = 662)
        diags.add_disk(id='fake-disk-id',
                       read_bytes=262144,
                       read_requests=112,
                       write_bytes=5778432,
                       write_requests=488)
        diags.memory_details.maximum = 524288
        return diags

    def get_all_bw_counters(self, instances):
        """Return bandwidth usage counters for each interface on each
           running VM.
        """
        bw = []
        for instance in instances:
            bw.append({'uuid': instance.uuid,
                       'mac_address': 'fa:16:3e:4c:2c:30',
                       'bw_in': 0,
                       'bw_out': 0})
        return bw

    def get_all_volume_usage(self, context, compute_host_bdms):
        """Return usage info for volumes attached to vms on
           a given host.
        """
        volusage = []
        return volusage

    def get_host_cpu_stats(self):
        stats = {'kernel': 5664160000000,
                'idle': 1592705190000000,
                'user': 26728850000000,
                'iowait': 6121490000000}
        stats['frequency'] = 800
        return stats

    def block_stats(self, instance, disk_id):
        return [0, 0, 0, 0, None]

    def macs_for_instance(self, instance):
        macs = []
        try:
            r = requests.get("http://{bampi_ip_addr}:{bampi_port}{bampi_api_base_url}/servers/{hostname}"
                                .format(bampi_ip_addr=BAMPI_IP_ADDR,
                                        bampi_port=BAMPI_PORT,
                                        bampi_api_base_url=BAMPI_API_BASE_URL,
                                        hostname=instance.hostname),
                             auth=HTTPBasicAuth(BAMPI_USER, BAMPI_PASS))
            r.raise_for_status()
        except requests.exceptions.HTTPError as e:
            LOG.warn(_LW("[BAMPI] %s" % e), instance=instance)
            return None
        else:
            mac = r.json()['macAddress']
            macs.append(mac)

        return set(macs)


    def get_console_output(self, context, instance):

        def _get_last_task_id(hostname):
            try:
                r = requests.get("http://{bampi_ip_addr}:{bampi_port}{bampi_api_base_url}/tasks"
                                .format(bampi_ip_addr=BAMPI_IP_ADDR,
                                            bampi_port=BAMPI_PORT,
                                            bampi_api_base_url=BAMPI_API_BASE_URL),
                             auth=HTTPBasicAuth(BAMPI_USER, BAMPI_PASS),
                             params={'hostname': hostname})
                r.raise_for_status()
            except requests.exceptions.HTTPError as e:
                LOG.warn(_LW("[BAMPI] %s" % e), instance=instance)
                return -1

            try:
                t_id = r.json()[-1]['id']
            except KeyError:
                LOG.warn(_LW("[BAMPI] Cannot fetch task history of hostname=%s" %
                    hostname), instance=instance)
                t_id = -1

            return t_id

        t_id = _get_last_task_id(instance.hostname)
        if t_id == -1:
            return 'EMPTY'

        try:
            r = requests.get("http://{bampi_ip_addr}:{bampi_port}{bampi_api_base_url}/tasks/{task_id}/log"
                                .format(bampi_ip_addr=BAMPI_IP_ADDR,
                                        bampi_port=BAMPI_PORT,
                                        bampi_api_base_url=BAMPI_API_BASE_URL,
                                        task_id=t_id),
                             auth=HTTPBasicAuth(BAMPI_USER, BAMPI_PASS))
            r.raise_for_status()
        except requests.exceptions.HTTPError as e:
            LOG.warn(_LW("[BAMPI] %s" % e), instance=instance)
            return 'CANNOT LOAD TASK LOG'
        else:
            raw_log = r.json().get('log', 'NO OUTPUT')

        # Post-processing log content
        log = raw_log.replace('\\n', '\n')

        return log

    def get_vnc_console(self, context, instance):
        return ctype.ConsoleVNC(internal_access_path='FAKE',
                                host='fakevncconsole.com',
                                port=6969)

    def get_spice_console(self, context, instance):
        return ctype.ConsoleSpice(internal_access_path='FAKE',
                                  host='fakespiceconsole.com',
                                  port=6969,
                                  tlsPort=6970)

    def get_rdp_console(self, context, instance):
        return ctype.ConsoleRDP(internal_access_path='FAKE',
                                host='fakerdpconsole.com',
                                port=6969)

    def get_serial_console(self, context, instance):
        return ctype.ConsoleSerial(internal_access_path='FAKE',
                                   host='fakerdpconsole.com',
                                   port=6969)

    def get_mks_console(self, context, instance):
        return ctype.ConsoleMKS(internal_access_path='FAKE',
                                host='fakemksconsole.com',
                                port=6969)

    def get_console_pool_info(self, console_type):
        return {'address': '127.0.0.1',
                'username': 'fakeuser',
                'password': 'fakepassword'}

    def refresh_security_group_rules(self, security_group_id):
        return True

    def refresh_instance_security_rules(self, instance):
        return True

    def get_available_resource(self, nodename):
        """Updates compute manager resource info on ComputeNode table.

           Since we don't have a real hypervisor, pretend we have lots of
           disk and ram.
        """
        cpu_info = collections.OrderedDict([
            ('arch', 'x86_64'),
            ('model', 'Nehalem'),
            ('vendor', 'Intel'),
            ('features', ['pge', 'clflush']),
            ('topology', {
                'cores': 1,
                'threads': 1,
                'sockets': 4,
                }),
            ])
        if nodename not in _FAKE_NODES:
            return {}

        host_status = self.host_status_base.copy()
        host_status.update(self.resources.dump())
        host_status['hypervisor_hostname'] = nodename
        host_status['host_hostname'] = nodename
        host_status['host_name_label'] = nodename
        host_status['cpu_info'] = jsonutils.dumps(cpu_info)
        return host_status

    def ensure_filtering_rules_for_instance(self, instance, network_info):
        return

    def get_instance_disk_info(self, instance, block_device_info=None):
        return

    def live_migration(self, context, instance, dest,
                       post_method, recover_method, block_migration=False,
                       migrate_data=None):
        post_method(context, instance, dest, block_migration,
                            migrate_data)
        return

    def live_migration_force_complete(self, instance):
        return

    def live_migration_abort(self, instance):
        return

    def cleanup_live_migration_destination_check(self, context,
                                                 dest_check_data):
        return

    def check_can_live_migrate_destination(self, context, instance,
                                           src_compute_info, dst_compute_info,
                                           block_migration=False,
                                           disk_over_commit=False):
        return {}

    def check_can_live_migrate_source(self, context, instance,
                                      dest_check_data, block_device_info=None):
        return

    def finish_migration(self, context, migration, instance, disk_info,
                         network_info, image_meta, resize_instance,
                         block_device_info=None, power_on=True):
        return

    def confirm_migration(self, migration, instance, network_info):
        return

    def pre_live_migration(self, context, instance, block_device_info,
                           network_info, disk_info, migrate_data):
        return

    def unfilter_instance(self, instance, network_info):
        return

    def _test_remove_vm(self, instance_uuid):
        """Removes the named VM, as if it crashed. For testing."""
        self.instances.pop(instance_uuid)

    def host_power_action(self, action):
        """Reboots, shuts down or powers up the host."""
        return action

    def host_maintenance_mode(self, host, mode):
        """Start/Stop host maintenance window. On start, it triggers
        guest VMs evacuation.
        """
        if not mode:
            return 'off_maintenance'
        return 'on_maintenance'

    def set_host_enabled(self, enabled):
        """Sets the specified host's ability to accept new instances."""
        if enabled:
            return 'enabled'
        return 'disabled'

    def get_volume_connector(self, instance):
        return {'ip': CONF.my_block_storage_ip,
                'initiator': 'fake',
                'host': 'fakehost'}

    def get_available_nodes(self, refresh=False):
        return _FAKE_NODES

    def instance_on_disk(self, instance):
        return False

    def quiesce(self, context, instance, image_meta):
        pass

    def unquiesce(self, context, instance, image_meta):
        pass


class FakeVirtAPI(virtapi.VirtAPI):
    @contextlib.contextmanager
    def wait_for_instance_event(self, instance, event_names, deadline=300,
                                error_callback=None):
        # NOTE(danms): Don't actually wait for any events, just
        # fall through
        yield


class SmallFakeDriver(FakeDriver):
    # The api samples expect specific cpu memory and disk sizes. In order to
    # allow the FakeVirt driver to be used outside of the unit tests, provide
    # a separate class that has the values expected by the api samples. So
    # instead of requiring new samples every time those
    # values are adjusted allow them to be overwritten here.

    vcpus = 1
    memory_mb = 8192
    local_gb = 1028
