from oslo_config import cfg

bampi_group = cfg.OptGroup(
    'bampi',
    title='BAMPI Options',
    help='Configuration options for BAMPI driver (Bare Metal).'
)

bampi_options = [
    cfg.StrOpt(
        'bampi_endpoint',
        help='URL override for the BAMPI API endpoint.'),
    cfg.StrOpt(
        'bampi_username',
        help='BAMPI admin name.'),
    cfg.StrOpt(
        'bampi_password',
        secret=True,
        help='BAMPI admin password.'),
    cfg.StrOpt(
        'peregrine_endpoint',
        help='Peregrine API endpoint.'),
    cfg.StrOpt(
        'peregrine_username',
        help='Peregrine admin name.'),
    cfg.StrOpt(
        'peregrine_password',
        secret=True,
        help='Peregrine admin password.'),
    cfg.StrOpt(
        'haas_core_endpoint',
        help='HaaS-core API endpoint. '),
    cfg.StrOpt(
        'haas_core_username',
        help='HaaS-core admin name.'),
    cfg.StrOpt(
        'haas_core_password',
        secret=True,
        help='HaaS-core admin password.'),
    cfg.StrOpt(
        'dummy_image_name',
        default='clonezilla-live-install-less_image.iso',
        help='No OS image name'),
    cfg.IntOpt(
        'provision_vlan_id',
        default=41,
        help='Dedicated VLAN ID for provisioning purpose'),
    cfg.StrOpt(
        'backup_directory',
        default='/tmp/snapshots',
        help='Temporary directory for backup images'),
]


def register_opts(conf):
    conf.register_group(bampi_group)
    conf.register_opts(bampi_options, group=bampi_group)


def list_opts():
    return {bampi_group: bampi_options}
