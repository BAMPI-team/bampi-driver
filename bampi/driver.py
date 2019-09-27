"""
A driver acting as a hypervisor + wrapping the BAMPI API, such that Nova may
provision bare metal resources.

"""

import collections
import contextlib
import copy
import os
import uuid


from oslo_log import log as logging
from oslo_serialization import jsonutils
from oslo_service import loopingcall
from oslo_utils import fileutils
from oslo_utils import versionutils

from nova.compute import power_state
from nova.compute import task_states
import nova.conf
from nova.console import type as ctype
from nova import exception
from nova.i18n import _LI
from nova.i18n import _LW
from nova.i18n import _LE
from nova import objects
from nova.objects import diagnostics as diagnostics_obj
from nova.objects import fields
from nova.virt import driver
from nova.virt import hardware
from nova.virt import virtapi
from nova import image
from nova import utils

import requests
from requests.auth import HTTPBasicAuth

CONF = nova.conf.CONF

LOG = logging.getLogger(__name__)


_BAMPI_NODES = None


# Power state mapping
power_state_map = {
    'on': power_state.RUNNING,
    'off': power_state.SHUTDOWN,
    'unknown': power_state.NOSTATE
}


def set_nodes(nodes):
    """Sets BampiDriver's node.list.

    It has effect on the following methods:
        get_available_nodes()
        get_available_resource

    To restore the change, call restore_nodes()
    """
    global _BAMPI_NODES
    _BAMPI_NODES = nodes


def restore_nodes():
    """Resets BampiDriver's node list modified by set_nodes().

    Usually called from tearDown().
    """
    global _BAMPI_NODES
    _BAMPI_NODES = [CONF.host]


class BampiInstance(object):

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


class BampiDriver(driver.ComputeDriver):
    capabilities = {
        "has_imagecache": False,
        "supports_recreate": True,
        "supports_migrate_to_same_host": False,
        "supports_attach_interface": True,
        "supports_tagged_attach_interface": True,
        "supports_tagged_attach_volume": True,
        "supports_extend_volume": True,
        "supports_multiattach": True
    }

    # Since we don't have a real hypervisor, pretend we have lots of
    # disk and ram so this driver can be used to test large instances.
    vcpus = 1000
    memory_mb = 800000
    local_gb = 600000

    """Hypervisor driver for BAMPI - bare-metal provisioning"""

    def __init__(self, virtapi, read_only=False):
        super(BampiDriver, self).__init__(virtapi)
        self.instances = {}
        self.resources = Resources(
            vcpus=self.vcpus,
            memory_mb=self.memory_mb,
            local_gb=self.local_gb)
        self.host_status_base = {
          'hypervisor_type': 'baremetal',
          'hypervisor_version': versionutils.convert_version_to_int('1.0'),
          'hypervisor_hostname': CONF.host,
          'cpu_info': {},
          'disk_available_least': 0,
          'supported_instances': [(
              fields.Architecture.X86_64,
              fields.HVType.BAREMETAL,
              fields.VMMode.HVM)],
          'numa_topology': None,
          }
        self._mounts = {}
        self._interfaces = {}
        self.active_migrations = {}
        self._image_api = image.API()
        self._nodes = self._init_nodes()

    def _init_nodes(self):
        if not _BAMPI_NODES:
            set_nodes([CONF.host])
        return copy.copy(_BAMPI_NODES)

    def init_host(self, host):
        return

    def list_instances(self):
        return [self.instances[uuid].name for uuid in self.instances.keys()]

    def list_instance_uuids(self):
        return list(self.instances.keys())

    def plug_vifs(self, instance, network_info):
        """Plug VIFs into networks."""
        pass

    def unplug_vifs(self, instance, network_info):
        """Unplug VIFs from networks."""
        pass

    def rebuild(self, context, instance, image_meta, injected_files,
                admin_password, bdms, detach_block_devices,
                attach_block_devices, network_info=None,
                recreate=False, block_device_info=None,
                preserve_ephemeral=False):
        """Destroy and re-make this instance."""
        LOG.info(_LI("METHOD REBUILD START"), instance=instance)
        instance.task_state = task_states.REBUILD_SPAWNING
        instance.save(expected_task_state=[task_states.REBUILDING])
        LOG.info(_LI("METHOD REBUILD END"), instance=instance)

    def spawn(self, context, instance, image_meta, injected_files,
              admin_password, allocations, network_info=None,
              block_device_info=None):
        uuid = instance.uuid
        state = power_state.RUNNING
        flavor = instance.flavor
        self.resources.claim(
            vcpus=flavor.vcpus,
            mem=flavor.memory_mb,
            disk=flavor.root_gb)
        bampi_instance = BampiInstance(instance.name, state, uuid)
        self.instances[uuid] = bampi_instance

        # XXX: Where dirty hack begins
        def _get_task_status(t_id):
            r = requests.get("{bampi_endpoint}/tasks/{task_id}"
                                .format(bampi_endpoint=CONF.bampi.bampi_endpoint,
                                        task_id=t_id),
                             auth=HTTPBasicAuth(CONF.bampi.bampi_username, CONF.bampi.bampi_password))
            t_status = r.json()['status']
            return t_status

        def _wait_for_ready():
            """Called at an interval until the task is successfully ended."""
            status = _get_task_status(t_id)
            LOG.debug(_LI("[BAMPI] Task %(task_id)s status=%(status)s"),
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
        r = requests.get("{bampi_endpoint}/servers"
                            .format(bampi_endpoint=CONF.bampi.bampi_endpoint),
                         auth=HTTPBasicAuth(CONF.bampi.bampi_username, CONF.bampi.bampi_password))
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
        if image_meta.name == CONF.bampi.dummy_image_name:
            LOG.info(_LI("[BAMPI] Dummy RestoreOS on hostname=%s"), instance.hostname, instance=instance)
            try:
                r = requests.get("{bampi_endpoint}/servers/{hostname}/powerStatus"
                                    .format(bampi_endpoint=CONF.bampi.bampi_endpoint,
                                            hostname=instance.hostname),
                                 auth=HTTPBasicAuth(CONF.bampi.bampi_username, CONF.bampi.bampi_password))
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

            # Differentiate between golden image and user backup image
            # User backup image does not need network configuration
            if task['taskType'] == 'restore_os' and image_meta.disk_format == 'raw':
                task['options'] = 'default'

            r = requests.post('{bampi_endpoint}/tasks'
                                .format(bampi_endpoint=CONF.bampi.bampi_endpoint),
                              auth=HTTPBasicAuth(CONF.bampi.bampi_username, CONF.bampi.bampi_password), json=task)
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

        if image_meta.name == CONF.bampi.dummy_image_name:
            self.reboot(context, instance, network_info, 'HARD')
        LOG.info(_LI("[BAMPI] All provision tasks have ended successfully."),
                 instance=instance)


        # Retrieve network information from Peregrine-H for target server
        LOG.info(_LI("[PEREGRINE] REQ => getAllHaasServerInfo..."),
                 instance=instance)
        r = requests.get("{peregrine_endpoint}/v2v/getAllHaasServerInfo"
                            .format(peregrine_endpoint=CONF.bampi.peregrine_endpoint),
                          auth=HTTPBasicAuth(CONF.bampi.peregrine_username, CONF.bampi.peregrine_password))
        if r.status_code == 200:
            LOG.info(_LI("[PEREGRINE] HaaS server info retrieved successfully."),
                     instance=instance)
        else:
            LOG.error(_LE("[PEREGRINE] ret_code=%s"),
                      r.status_code,
                      instance=instance)
            raise exception.NovaException("Cannot retrieve HaaS server info from Peregrine-H.")

        # Construct reverse map of MAC address (key) and dictionary of port
        # group name, connected switch ip, and connected switch port (value)
        mac_map = {}
        for info in r.json()['bulkRequest']:
            if info['admin_server_name'] != instance.display_name:
                continue
            for port_group in info['port_group_list']:
                for port in port_group['port_list']:
                    LOG.info(_LI("Found port MAC: %s with dictionary of port "
                                 "group name: %s, connected switch ip: %s, connected"
                                 "switch port: %s."),
                             port['port_mac'], port_group['port_group_name'],
                             port['connected_sw_ip'],
                             port['connected_sw_port'],
                             instance=instance)
                    mac_map[port['port_mac']] = {
                            'pg_name': port_group['port_group_name'],
                            'sw_ip': port['connected_sw_ip'],
                            'sw_port': port['connected_sw_port']
                    }

        network_provision_payload = {}
        ni = jsonutils.loads(network_info.json())
        for vif in ni:
            tnid = vif['network']['id']
            mac_address = vif['address']
            LOG.info(_LI("tenant network id = %s, mac address = %s"),
                     tnid, mac_address, instance=instance)

            # Get segmentation ID of desired tenant networks from HaaS-core
            LOG.info(_LI("[HAAS_CORE] REQ => network_detail..."), instance=instance)
            r = requests.get("{haas_core_endpoint}/network/get/{tenant_network_id}"
                                .format(haas_core_endpoint=CONF.bampi.haas_core_endpoint,
                                        tenant_network_id=tnid),
                             auth=HTTPBasicAuth(CONF.bampi.haas_core_username, CONF.bampi.haas_core_password))
            if r.status_code == 200:
                data = r.json()['data']
                nd = jsonutils.loads(data)
                segmentation_id = nd['provider:segmentation_id']
                LOG.info(_LI("segmentation id = %s"),
                         segmentation_id, instance=instance)
                network_provision_payload = {
                    'provision_vlan': {
                        'admin_server_name': instance.display_name,
                        'port_group_name': mac_map[mac_address]['pg_name'],
                        'untagged_vlan': segmentation_id,
                        'tagged_vlans': []
                    }
                }
            else:
                LOG.error(_LE("[HAAS_CORE] ret_code=%s"),
                          r.status_code,
                          instance=instance)
                raise exception.NovaException("Cannot get segmentation_id from HaaS-core. Abort instance spawning...")

            # Provision VLANs for tenant networks
            LOG.info(_LI("[PEREGRINE] REQ => networkProvision..."),
                     instance=instance)
            r = requests.post("{peregrine_endpoint}/networkprovision/setVlan"
                                .format(peregrine_endpoint=CONF.bampi.peregrine_endpoint),
                              auth=HTTPBasicAuth(CONF.bampi.peregrine_username, CONF.bampi.peregrine_password),
                              json=network_provision_payload)
            if r.status_code == 200:
                LOG.info(_LI("[PEREGRINE] Tenant network set successfully."),
                         instance=instance)
                del mac_map[mac_address]
            else:
                LOG.error(_LE("[PEREGRINE] ret_code=%s"),
                          r.status_code,
                          instance=instance)
                raise exception.NovaException("Cannot set tenant VLAN using Peregrine-H. Abort instance spawning...")

        # Shutdown unused switch port(s)
        for mac in mac_map:
            r = requests.put("{peregrine_endpoint}/networkprovision/setPortStateOff/{sw_ip}/{sw_port}"
                                .format(peregrine_endpoint=CONF.bampi.peregrine_endpoint,
                                        sw_ip=mac_map[mac]['sw_ip'],
                                        sw_port=mac_map[mac]['sw_port']),
                              auth=HTTPBasicAuth(CONF.bampi.peregrine_username, CONF.bampi.peregrine_password))
            if r.status_code == 200:
                if r.text != 'SUCCESS':
                    LOG.error(_LE("[PEREGRINE] Failed to set "
                                  "%(sw_ip)s:%(sw_port)s state off."),
                              {'sw_ip': mac_map[mac]['sw_ip'],
                               'sw_port': mac_map[mac]['sw_port']},
                              instance=instance)
                    raise exception.NovaException("Cannot shutdown switch port "
                            "using Peregrine-H. Abort instance spawning...")
                else:
                    LOG.info(_LI("[PEREGRINE] Unused switch port "
                             "%(sw_ip)s:%(sw_port)s shutdown successfully."),
                             {'sw_ip': mac_map[mac]['sw_ip'],
                              'sw_port': mac_map[mac]['sw_port']},
                             instance=instance)
            else:
                LOG.error(_LE("[PEREGRINE] ret_code=%s"),
                          r.status_code,
                          instance=instance)
                raise exception.NovaException("Cannot shutdown switch port "
                        "using Peregrine-H. Abort instance spawning...")

    def _create_snapshot_metadata(self, image_meta, instance,
                                  img_fmt, snp_name, snp_desc):
        metadata = {'is_public': False,
                    'status': 'active',
                    'name': snp_name,
                    'properties': {
                                   'description': snp_desc,
                                   'kernel_id': instance.kernel_id,
                                   'image_location': 'snapshot',
                                   'image_state': 'available',
                                   'owner_id': instance.project_id,
                                   'ramdisk_id': instance.ramdisk_id,
                                   }
                    }
        if instance.os_type:
            metadata['properties']['os_type'] = instance.os_type

        if image_meta.disk_format == 'ami':
            metadata['disk_format'] = 'ami'
        else:
            metadata['disk_format'] = img_fmt

        if image_meta.obj_attr_is_set("container_format"):
            metadata['container_format'] = image_meta.container_format
        else:
            metadata['container_format'] = "bare"

        return metadata

    def snapshot(self, context, instance, image_id, update_task_state):
        if instance.uuid not in self.instances:
            raise exception.InstanceNotRunning(instance_id=instance.uuid)
        snapshot = self._image_api.get(context, image_id)
        image_format = 'raw'
        snapshot_name = uuid.uuid4().hex[:8]
        metadata = self._create_snapshot_metadata(instance.image_meta,
                                                  instance,
                                                  image_format,
                                                  snapshot_name,
                                                  snapshot['name'])

        update_task_state(task_state=task_states.IMAGE_PENDING_UPLOAD)
        LOG.info(_LI("Image pending upload..."), instance=instance)

        snapshot_directory = CONF.bampi.backup_directory
        fileutils.ensure_tree(snapshot_directory)

        def _get_task_status(t_id):
            r = requests.get("{bampi_endpoint}/tasks/{task_id}"
                                .format(bampi_endpoint=CONF.bampi.bampi_endpoint,
                                        task_id=t_id),
                             auth=HTTPBasicAuth(CONF.bampi.bampi_username, CONF.bampi.bampi_password))
            t_status = r.json()['status']
            return t_status

        def _wait_for_ready():
            """Called at an interval until the task is successfully ended."""
            status = _get_task_status(t_id)
            LOG.debug(_LI("[BAMPI] Task %(task_id)s status=%(status)s"),
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

        backup_tasks_q = [{
                    'taskType': 'change_boot_mode',
                    'taskProfile': 'PXE'
                }, {
                    'taskType': 'disposable_os_run_script',
                    'taskProfile': 'haas_backup',
                    'options': snapshot_name
                }, {
                    'taskType': 'change_boot_mode',
                    'taskProfile': 'Disabled'
                }]

        # Iterate through all tasks in backup task queue
        for task in backup_tasks_q:
            task['hostname'] = instance.display_name

            # Requesting outer service to execute the task
            LOG.info(_LI("[BAMPI] REQ => Starting backup task %s..."),
                     task['taskType'], instance=instance)
            r = requests.post('{bampi_endpoint}/tasks'
                                .format(bampi_endpoint=CONF.bampi.bampi_endpoint),
                              auth=HTTPBasicAuth(CONF.bampi.bampi_username, CONF.bampi.bampi_password), json=task)
            try:
                t_id = r.json()['id']
            except KeyError:
                # If we cannot find 'id' in return json...
                LOG.error(_LE("[BAMPI] RESP (backup) => task_type=%s, task_profile=%s, ret_code=%s"),
                          task['taskType'], task['taskProfile'], r.status_code,
                          instance=instance)
                raise exception.NovaException("BAMPI cannot execute task. Abort instance backup...")
            else:
                LOG.info(_LI("[BAMPI] RESP (backup) => ret_code=%s, task_id=%s"),
                         r.status_code, t_id, instance=instance)

            # Polling for task status
            time = loopingcall.FixedIntervalLoopingCall(_wait_for_ready)
            ret = time.start(interval=30).wait()

            # Task failed, abort spawning
            if ret == False:
                raise exception.NovaException("Backup task failed. Abort instance backup...")
            else:
                LOG.info(_LI("[BAMPI] Backup task %s:%s has ended successfully."),
                         task['taskType'],
                         task['taskProfile'],
                         instance=instance)

        LOG.info(_LI("[BAMPI] All backup tasks have ended successfully."),
                 instance=instance)

        with utils.tempdir(dir=snapshot_directory) as tmpdir:
            # Retrieve snapshot image from baremetal service
            image_name = 'clonezilla-live-{snapshot_name}.iso'.format(snapshot_name=snapshot_name)
            out_path = os.path.join(tmpdir, snapshot_name)
            download_url = '{bampi_image_endpoint}/{image_name}'.format(
                    bampi_image_endpoint=CONF.bampi.bampi_image_endpoint,
                    image_name=image_name)
            r = requests.get(download_url, stream=True)
            with open(out_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            LOG.info(_LI("Snapshot created, beginning image upload"),
                         instance=instance)

            # Upload that image to the image service
            update_task_state(task_state=task_states.IMAGE_UPLOADING,
                    expected_state=task_states.IMAGE_PENDING_UPLOAD)
            LOG.info(_LI("Image uploading with metadata=%(metadata)s, "
                         "snapshot_directory=%(snapshot_directory)s"),
                     {
                         'metadata': metadata,
                         'snapshot_directory': snapshot_directory
                     },
                     instance=instance)
            with open(out_path) as image_file:
                self._image_api.update(context,
                                       image_id,
                                       metadata,
                                       image_file)

        LOG.info(_LI("Snapshot image upload complete"), instance=instance)

        # Notify upper service to take the rest when backup task is done
        self._post_snapshot(instance)

    def _post_snapshot(self, instance):
        LOG.info(_LI("[HAAS_CORE] REQ => Backup callback..."),
                 instance=instance)
        r = requests.put('{haas_core_endpoint}/servers/{hostname}/backupCallback'
                            .format(haas_core_endpoint=CONF.bampi.haas_core_endpoint,
                                    hostname=instance.hostname),
                         auth=HTTPBasicAuth(CONF.bampi.haas_core_username, CONF.bampi.haas_core_password))

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
            r = requests.put("{bampi_endpoint}/servers/{hostname}/powerStatus"
                                .format(bampi_endpoint=CONF.bampi.bampi_endpoint,
                                        hostname=instance.hostname),
                             auth=HTTPBasicAuth(CONF.bampi.bampi_username, CONF.bampi.bampi_password),
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
        self.instances[instance.uuid].state = power_state.RUNNING

    def poll_rebooting_instances(self, timeout, instances):
        pass

    def migrate_disk_and_power_off(self, context, instance, dest,
                                   flavor, network_info,
                                   block_device_info=None,
                                   timeout=0, retry_interval=0):
        pass

    def finish_revert_migration(self, context, instance, network_info,
                                block_device_info=None, power_on=True):
        self.instances[instance.uuid] = FakeInstance(
            instance.name, power_state.RUNNING, instance.uuid)

    def post_live_migration_at_destination(self, context, instance,
                                           network_info,
                                           block_migration=False,
                                           block_device_info=None):
        pass

    def power_off(self, instance, timeout=0, retry_interval=0):
        LOG.info(_LI("[BAMPI] Power off hostname=%s" % instance.hostname),
                 instance=instance)

        # Check power state again
        try:
            r = requests.get("{bampi_endpoint}/servers/{hostname}/powerStatus"
                                .format(bampi_endpoint=CONF.bampi.bampi_endpoint,
                                        hostname=instance.hostname),
                             auth=HTTPBasicAuth(CONF.bampi.bampi_username, CONF.bampi.bampi_password))
            r.raise_for_status()
        except requests.exceptions.HTTPError as e:
            LOG.warn(_LW("[BAMPI] %s" % e), instance=instance)
        else:
            p_st = r.json()['status']

        # NOTE: Prevent machines being powered off by power status check
        # mechanism
        if instance.vm_state == 'stopped' and instance.power_state == power_state.RUNNING:
            LOG.warn(_LW("Forcing machine %s to be powered off is not allowed" % instance.hostname),
                     instance=instance)
            return
        # Sometimes the BMC may behave abnormally, we'll check its power state again
        elif instance.vm_state == 'active' and instance.power_state == power_state.SHUTDOWN:
            if power_state_map[p_st] == power_state.RUNNING:
                LOG.warn(_LW("Power state false alarm, we don't power off the server"), instance=instance)
                return

        # Actually doing power off
        try:
            r = requests.put("{bampi_endpoint}/servers/{hostname}/powerStatus"
                                .format(bampi_endpoint=CONF.bampi.bampi_endpoint,
                                        hostname=instance.hostname),
                             auth=HTTPBasicAuth(CONF.bampi.bampi_username, CONF.bampi.bampi_password),
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
            r = requests.put("{bampi_endpoint}/servers/{hostname}/powerStatus"
                                .format(bampi_endpoint=CONF.bampi.bampi_endpoint,
                                        hostname=instance.hostname),
                             auth=HTTPBasicAuth(CONF.bampi.bampi_username, CONF.bampi.bampi_password),
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

    def _destroy(self, instance):
        self.power_off(instance)

        # Retrieve network information from Peregrine-H for target server
        LOG.info(_LI("[PEREGRINE] REQ => getAllHaasServerInfo..."),
                 instance=instance)
        r = requests.get("{peregrine_endpoint}/v2v/getAllHaasServerInfo"
                            .format(peregrine_endpoint=CONF.bampi.peregrine_endpoint),
                          auth=HTTPBasicAuth(CONF.bampi.peregrine_username, CONF.bampi.peregrine_password))
        if r.status_code == 200:
            LOG.info(_LI("[PEREGRINE] HaaS server info retrieved successfully."),
                     instance=instance)
        else:
            LOG.error(_LE("[PEREGRINE] ret_code=%s"),
                      r.status_code,
                      instance=instance)
            raise exception.NovaException("Cannot retrieve HaaS server info from Peregrine-H.")

        # Construct port group name list
        pgn_map = {}
        for info in r.json()['bulkRequest']:
            if info['admin_server_name'] == instance.display_name:
                for port_group in info['port_group_list']:
                    LOG.info(_LI("Found port group name: %s."),
                             port_group['port_group_name'],
                             instance=instance)
                    for port in port_group['port_list']:
                        pgn_map[port_group['port_group_name']] = {
                                'sw_ip': port['connected_sw_ip'],
                                'sw_port': port['connected_sw_port']
                        }
                break

        # Switch back to provision network
        network_provision_payload = {
            'provision_vlan': {
                'admin_server_name': instance.display_name,
                'port_group_name': '',
                'untagged_vlan': CONF.bampi.provision_vlan_id,
                'tagged_vlans': []
            }
        }
        for pgn in pgn_map:
            network_provision_payload['provision_vlan']['port_group_name'] = pgn

            # Change VLAN ID from tenant network to provision network
            LOG.info(_LI("[PEREGRINE] REQ => networkProvision..."),
                     instance=instance)
            r = requests.post("{peregrine_endpoint}/networkprovision/setVlan"
                                .format(peregrine_endpoint=CONF.bampi.peregrine_endpoint),
                              auth=HTTPBasicAuth(CONF.bampi.peregrine_username, CONF.bampi.peregrine_password),
                              json=network_provision_payload)
            if r.status_code == 200:
                LOG.info(_LI("[PEREGRINE] Provision network set successfully."),
                         instance=instance)
            else:
                LOG.error(_LE("[PEREGRINE] ret_code=%s"),
                          r.status_code,
                          instance=instance)
                raise exception.NovaException("Cannot set provision VLAN using Peregrine-H. Abort instance spawning...")

        # No shutdown all switch ports
        for pgn in pgn_map:
            r = requests.put("{peregrine_endpoint}/networkprovision/setPortStateOn/{sw_ip}/{sw_port}"
                                .format(peregrine_endpoint=CONF.bampi.peregrine_endpoint,
                                        sw_ip=pgn_map[pgn]['sw_ip'],
                                        sw_port=pgn_map[pgn]['sw_port']),
                              auth=HTTPBasicAuth(CONF.bampi.peregrine_username, CONF.bampi.peregrine_password))
            if r.status_code == 200:
                if r.text != 'SUCCESS':
                    LOG.error(_LE("[PEREGRINE] Failed to set "
                                  "%(sw_ip)s:%(sw_port)s state on."),
                              {'sw_ip': pgn_map[pgn]['sw_ip'],
                               'sw_port': pgn_map[pgn]['sw_port']},
                              instance=instance)
                    raise exception.NovaException("Cannot shutdown switch port "
                            "using Peregrine-H. Abort instance spawning...")
                else:
                    LOG.info(_LI("[PEREGRINE] Switch port "
                             "%(sw_ip)s:%(sw_port)s no shutdown successfully."),
                             {'sw_ip': pgn_map[pgn]['sw_ip'],
                              'sw_port': pgn_map[pgn]['sw_port']},
                             instance=instance)
            else:
                LOG.error(_LE("[PEREGRINE] ret_code=%s"),
                          r.status_code,
                          instance=instance)
                raise exception.NovaException("Cannot no shutdown switch port "
                        "using Peregrine-H. Abort instance spawning...")


    def destroy(self, context, instance, network_info, block_device_info=None,
                destroy_disks=True):

        key = instance.uuid
        if key in self.instances:
            self._destroy(instance)
            self.cleanup(context, instance, network_info, block_device_info,
                         destroy_disks, None)
        else:
            LOG.warn(_LW("Key '%(key)s' not in instances '%(inst)s'"),
                        {'key': key,
                         'inst': self.instances}, instance=instance)

    def _undefine(self, instance):
        flavor = instance.flavor
        self.resources.release(
            vcpus=flavor.vcpus,
            mem=flavor.memory_mb,
            disk=flavor.root_gb)
        del self.instances[instance.uuid]

    def cleanup(self, context, instance, network_info, block_device_info=None,
                destroy_disks=True, migrate_data=None, destroy_vifs=True):
        def _get_task_status(t_id):
            r = requests.get("{bampi_endpoint}/tasks/{task_id}"
                                .format(bampi_endpoint=CONF.bampi.bampi_endpoint,
                                        task_id=t_id),
                             auth=HTTPBasicAuth(CONF.bampi.bampi_username, CONF.bampi.bampi_password))
            t_status = r.json()['status']
            return t_status

        def _wait_for_ready():
            """Called at an interval until the task is successfully ended."""
            status = _get_task_status(t_id)
            LOG.debug(_LI("[BAMPI] Task %(task_id)s status=%(status)s"),
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
            r = requests.post('{bampi_endpoint}/tasks'
                                .format(bampi_endpoint=CONF.bampi.bampi_endpoint),
                              auth=HTTPBasicAuth(CONF.bampi.bampi_username, CONF.bampi.bampi_password), json=task)
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
        r = requests.put('{haas_core_endpoint}/servers/{hostname}/available'
                            .format(haas_core_endpoint=CONF.bampi.haas_core_endpoint,
                                    hostname=instance.hostname),
                         auth=HTTPBasicAuth(CONF.bampi.haas_core_username, CONF.bampi.haas_core_password))

        # Boot into disposable OS to stand-by
        LOG.info(_LI("[BAMPI] Power on hostname=%s to stand-by" % instance.hostname),
                 instance=instance)
        try:
            r = requests.put("{bampi_endpoint}/servers/{hostname}/powerStatus"
                                .format(bampi_endpoint=CONF.bampi.bampi_endpoint,
                                        hostname=instance.hostname),
                             auth=HTTPBasicAuth(CONF.bampi.bampi_username, CONF.bampi.bampi_password),
                             json={'status': 'on'})
            r.raise_for_status()
        except requests.exceptions.HTTPError as e:
            LOG.warn(_LW("[BAMPI] %s" % e), instance=instance)

        self._undefine(instance)

    def attach_volume(self, context, connection_info, instance, mountpoint,
                      disk_bus=None, device_type=None, encryption=None):
        """Attach the disk to the instance at mountpoint using info."""
        instance_name = instance.name
        if instance_name not in self._mounts:
            self._mounts[instance_name] = {}
        self._mounts[instance_name][mountpoint] = connection_info

    def detach_volume(self, conext, connection_info, instance, mountpoint,
                      encryption=None):
        """Detach the disk attached to the instance."""
        try:
            del self._mounts[instance.name][mountpoint]
        except KeyError:
            pass

    def swap_volume(self, context, old_connection_info, new_connection_info,
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

    def detach_interface(self, context, instance, vif):
        try:
            del self._interfaces[vif['id']]
        except KeyError:
            raise exception.InterfaceDetachFailed(
                    instance_uuid=instance.uuid)

    def get_info(self, instance):
        if instance.uuid not in self.instances:
            # Instance not found may caused by bampi driver memory lost...
            try:
                r = requests.get("{bampi_endpoint}/servers/{hostname}/powerStatus"
                                    .format(bampi_endpoint=CONF.bampi.bampi_endpoint,
                                            hostname=instance.hostname),
                                 auth=HTTPBasicAuth(CONF.bampi.bampi_username, CONF.bampi.bampi_password))
                r.raise_for_status()
            except requests.exceptions.HTTPError as e:
                LOG.warn(_LW("[BAMPI] %s" % e), instance=instance)
                raise exception.InstanceNotFound(instance_id=instance.uuid)
            else:
                p_st = r.json()['status']
                if p_st == 'unknown':
                    # Default to SHUTDOWN
                    state = power_state.SHUTDOWN
                else:
                    state = power_state_map[p_st]

                # Construct the lost bampi instance...
                bampi_instance = BampiInstance(instance.name, state, instance.uuid)
                self.instances[instance.uuid] = bampi_instance

                return hardware.InstanceInfo(state=state)

        i = self.instances[instance.uuid]

        LOG.debug(_LI("[BAMPI] get_info hostname=%s" % instance.hostname),
                 instance=instance)
        try:
            r = requests.get("{bampi_endpoint}/servers/{hostname}/powerStatus"
                                .format(bampi_endpoint=CONF.bampi.bampi_endpoint,
                                        hostname=instance.hostname),
                             auth=HTTPBasicAuth(CONF.bampi.bampi_username, CONF.bampi.bampi_password))
            r.raise_for_status()
        except requests.exceptions.HTTPError as e:
            LOG.warn(_LW("[BAMPI] %s" % e), instance=instance)
        else:
            p_st = r.json()['status']
            i.state = power_state_map[p_st]

        return hardware.InstanceInfo(state=i.state)

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
        diags = diagnostics_obj.Diagnostics(state='running', driver='bampi',
                hypervisor_os='bampi-os', uptime=46664, config_drive=True)
        diags.add_cpu(time=17300000000)
        diags.add_nic(mac_address='01:23:45:67:89:ab',
                      rx_packets=26701,
                      rx_octets=2070139,
                      tx_octets=140208,
                      tx_packets = 662)
        diags.add_disk(id='bampi-disk-id',
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

    def get_console_output(self, context, instance):

        def _get_last_task_id(hostname):
            try:
                r = requests.get("{bampi_endpoint}/tasks"
                                .format(bampi_endpoint=CONF.bampi.bampi_endpoint),
                             auth=HTTPBasicAuth(CONF.bampi.bampi_username, CONF.bampi.bampi_password),
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
            r = requests.get("{bampi_endpoint}/tasks/{task_id}/log"
                                .format(bampi_endpoint=CONF.bampi.bampi_endpoint,
                                        task_id=t_id),
                             auth=HTTPBasicAuth(CONF.bampi.bampi_username, CONF.bampi.bampi_password))
            r.raise_for_status()
        except requests.exceptions.HTTPError as e:
            LOG.warn(_LW("[BAMPI] %s" % e), instance=instance)
            return 'CANNOT LOAD TASK LOG'
        else:
            raw_log = r.json().get('log', 'NO OUTPUT')

        # Post-processing log content
        log = raw_log.replace('\\n', '\n')

        return log

    def refresh_security_group_rules(self, security_group_id):
        return True

    def refresh_instance_security_rules(self, instance):
        return True

    def get_inventory(self, nodename):
        """Return a dict, keyed by resource class, of inventory information for
        the supplied node.
        """
        disk_gb = 600000
        memory_mb = 800000
        vcpus = 1000

        result = {
            fields.ResourceClass.VCPU: {
                'total': vcpus,
                'min_unit': 1,
                'max_unit': vcpus,
                'step_size': 1,
            },
            fields.ResourceClass.MEMORY_MB: {
                'total': memory_mb,
                'min_unit': 1,
                'max_unit': memory_mb,
                'step_size': 1,
            },
            fields.ResourceClass.DISK_GB: {
                'total': disk_gb,
                'min_unit': 1,
                'max_unit': disk_gb,
                'step_size': 1,
            },
        }

        return result

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
        if nodename not in self._nodes:
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
        """Confirms a resize/migration, destroying the source VM."""
        LOG.info(_LI("METHOD CONFIRM_MIGRATION START"), instance=instance)
        if instance.host != CONF.host:
            self._undefine(instance)
            self.unplug_vifs(instance, network_info)
            self.unfilter_instance(instance, network_info)

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
        pass

    def get_available_nodes(self, refresh=False):
        return _BAMPI_NODES

    def instance_on_disk(self, instance):
        return False

    def quiesce(self, context, instance, image_meta):
        pass

    def unquiesce(self, context, instance, image_meta):
        pass

    def network_binding_host_id(self, context, instance):
        """Get host ID to associate with network ports."""
        return instance.get('host')


class BampiVirtAPI(virtapi.VirtAPI):
    @contextlib.contextmanager
    def wait_for_instance_event(self, instance, event_names, deadline=300,
                                error_callback=None):
        # NOTE(danms): Don't actually wait for any events, just
        # fall through
        yield
