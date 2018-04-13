# Copyright 2017 Red Hat Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import json
import logging
import netaddr
import netifaces
import os
import subprocess
import sys
import yaml

from osc_lib.i18n import _

from oslo_utils import netutils
import psutil

from oslo_config import cfg

from string import replace

from tripleoclient import constants


class FailedValidation(Exception):
    pass

CONF = cfg.CONF

# We need 8 GB, leave a little room for variation in what 8 GB means on
# different platforms.
REQUIRED_MB = 7680
PASSWORD_PATH = '%s/%s' % (constants.UNDERCLOUD_OUTPUT_DIR,
                           'undercloud-passwords.conf')

LOG = logging.getLogger(__name__ + ".UndercloudSetup")


def _run_command(args, env=None, name=None):
    """Run the command defined by args and return its output

    :param args: List of arguments for the command to be run.
    :param env: Dict defining the environment variables. Pass None to use
        the current environment.
    :param name: User-friendly name for the command being run. A value of
        None will cause args[0] to be used.
    """
    if name is None:
        name = args[0]
    try:
        return subprocess.check_output(args,
                                       stderr=subprocess.STDOUT,
                                       env=env).decode('utf-8')
    except subprocess.CalledProcessError as e:
        message = '%s failed: %s' % (name, e.output)
        LOG.error(message)
        raise RuntimeError(message)


def _run_live_command(args, env=None, name=None):
    """Run the command defined by args and log its output

    Takes the same arguments as _run_command, but runs the process
    asynchronously so the output can be logged while the process is still
    running.
    """
    if name is None:
        name = args[0]
    process = subprocess.Popen(args, env=env,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
    while True:
        line = process.stdout.readline().decode('utf-8')
        if line:
            LOG.info(line.rstrip())
        if line == '' and process.poll() is not None:
            break
    if process.returncode != 0:
        message = '%s failed. See log for details.' % name
        LOG.error(message)
        raise RuntimeError(message)


def _check_hostname():
    """Check system hostname configuration

    Rabbit and Puppet require pretty specific hostname configuration. This
    function ensures that the system hostname settings are valid before
    continuing with the installation.
    """
    if CONF.undercloud_hostname is not None:
        args = ['sudo', 'hostnamectl', 'set-hostname',
                CONF.undercloud_hostname]
        _run_command(args, name='hostnamectl')

    LOG.info('Checking for a FQDN hostname...')
    args = ['sudo', 'hostnamectl', '--static']
    detected_static_hostname = _run_command(args, name='hostnamectl').rstrip()
    LOG.info('Static hostname detected as %s', detected_static_hostname)
    args = ['sudo', 'hostnamectl', '--transient']
    detected_transient_hostname = _run_command(args,
                                               name='hostnamectl').rstrip()
    LOG.info('Transient hostname detected as %s', detected_transient_hostname)
    if detected_static_hostname != detected_transient_hostname:
        LOG.error('Static hostname "%s" does not match transient hostname '
                  '"%s".', detected_static_hostname,
                  detected_transient_hostname)
        LOG.error('Use hostnamectl to set matching hostnames.')
        raise RuntimeError('Static and transient hostnames do not match')
    with open('/etc/hosts') as hosts_file:
        for line in hosts_file:
            if (not line.lstrip().startswith('#') and
                    detected_static_hostname in line.split()):
                break
        else:
            short_hostname = detected_static_hostname.split('.')[0]
            if short_hostname == detected_static_hostname:
                message = 'Configured hostname is not fully qualified.'
                LOG.error(message)
                raise RuntimeError(message)
            sed_cmd = ('sed -i "s/127.0.0.1\(\s*\)/127.0.0.1\\1%s %s /" '
                       '/etc/hosts' %
                       (detected_static_hostname, short_hostname))
            args = ['sudo', '/bin/bash', '-c', sed_cmd]
            _run_command(args, name='hostname-to-etc-hosts')
            LOG.info('Added hostname %s to /etc/hosts',
                     detected_static_hostname)


def _check_memory():
    """Check system memory

    The undercloud will not run properly in less than 8 GB of memory.
    This function verifies that at least that much is available before
    proceeding with install.
    """
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    total_mb = (mem.total + swap.total) / 1024 / 1024
    if total_mb < REQUIRED_MB:
        LOG.error('At least %d MB of memory is required for undercloud '
                  'installation.  A minimum of 8 GB is recommended. '
                  'Only detected %d MB' % (REQUIRED_MB, total_mb))
        raise RuntimeError('Insufficient memory available')


def _check_ipv6_enabled():
    """Test if IPv6 is enabled

    If /proc/net/if_inet6 exist ipv6 sysctl settings are available.
    """
    return os.path.isfile('/proc/net/if_inet6')


def _wrap_ipv6(ip):
    """Wrap a IP address in square brackets if IPv6

    """
    if netutils.is_valid_ipv6(ip):
        return "[%s]" % ip
    return ip


def _check_sysctl():
    """Check sysctl option availability

    The undercloud will not install properly if some of the expected sysctl
    values are not available to be set.
    """
    options = ['net.ipv4.ip_forward', 'net.ipv4.ip_nonlocal_bind']
    if _check_ipv6_enabled():
        options.append('net.ipv6.ip_nonlocal_bind')

    not_available = []
    for option in options:
        path = '/proc/sys/{opt}'.format(opt=option.replace('.', '/'))
        if not os.path.isfile(path):
            not_available.append(option)

    if not_available:
        LOG.error('Required sysctl options are not available. Check '
                  'that your kernel is up to date. Missing: {options}'
                  ' '.format(options=", ".join(not_available)))
        raise RuntimeError('Missing sysctl options')


def _validate_ips():
    def is_ip(value, param_name):
        try:
            netaddr.IPAddress(value)
        except netaddr.core.AddrFormatError:
            msg = '%s "%s" must be a valid IP address' % \
                  (param_name, value)
            LOG.error(msg)
            raise FailedValidation(msg)
    for ip in CONF.undercloud_nameservers:
        is_ip(ip, 'undercloud_nameservers')


def _validate_value_formats():
    """Validate format of some values

    Certain values have a specific format that must be maintained in order to
    work properly.  For example, local_ip must be in CIDR form, and the
    hostname must be a FQDN.
    """
    try:
        local_ip = netaddr.IPNetwork(CONF.local_ip)
        if local_ip.prefixlen == 32:
            LOG.error('Invalid netmask')
            raise netaddr.AddrFormatError('Invalid netmask')
        # If IPv6 the ctlplane network uses the EUI-64 address format,
        # which requires the prefix to be /64
        if local_ip.version == 6 and local_ip.prefixlen != 64:
            LOG.error('Prefix must be 64 for IPv6')
            raise netaddr.AddrFormatError('Prefix must be 64 for IPv6')
    except netaddr.core.AddrFormatError as e:
        message = ('local_ip "%s" not valid: "%s" '
                   'Value must be in CIDR format.' %
                   (CONF.local_ip, str(e)))
        LOG.error(message)
        raise FailedValidation(message)
    hostname = CONF['undercloud_hostname']
    if hostname is not None and '.' not in hostname:
        message = 'Hostname "%s" is not fully qualified.' % hostname
        LOG.error(message)
        raise FailedValidation(message)


def _validate_in_cidr(subnet_props, subnet_name):
    cidr = netaddr.IPNetwork(subnet_props.cidr)

    def validate_addr_in_cidr(addr, pretty_name=None, require_ip=True):
        try:
            if netaddr.IPAddress(addr) not in cidr:
                message = ('Config option %s "%s" not in defined CIDR "%s"' %
                           (pretty_name, addr, cidr))
                LOG.error(message)
                raise FailedValidation(message)
        except netaddr.core.AddrFormatError:
            if require_ip:
                message = 'Invalid IP address: %s' % addr
                LOG.error(message)
                raise FailedValidation(message)

    if subnet_name == CONF.local_subnet:
        validate_addr_in_cidr(str(netaddr.IPNetwork(CONF.local_ip).ip),
                              'local_ip')
    validate_addr_in_cidr(subnet_props.gateway, 'gateway')
    # NOTE(bnemec): The ui needs to be externally accessible, which means in
    # many cases we can't have the public vip on the provisioning network.
    # In that case users are on their own to ensure they've picked valid
    # values for the VIP hosts.
    if ((CONF.undercloud_service_certificate or
            CONF.generate_service_certificate) and
            not CONF.enable_ui):
        validate_addr_in_cidr(CONF['undercloud_public_host'],
                              'undercloud_public_host',
                              require_ip=False)
        validate_addr_in_cidr(CONF['undercloud_admin_host'],
                              'undercloud_admin_host',
                              require_ip=False)
    validate_addr_in_cidr(subnet_props.dhcp_start, 'dhcp_start')
    validate_addr_in_cidr(subnet_props.dhcp_end, 'dhcp_end')


def _validate_dhcp_range(subnet_props):
    start = netaddr.IPAddress(subnet_props.dhcp_start)
    end = netaddr.IPAddress(subnet_props.dhcp_end)
    if start >= end:
        message = ('Invalid dhcp range specified, dhcp_start "%s" does '
                   'not come before dhcp_end "%s"' % (start, end))
        LOG.error(message)
        raise FailedValidation(message)


def _validate_inspection_range(subnet_props):
    start = netaddr.IPAddress(subnet_props.inspection_iprange.split(',')[0])
    end = netaddr.IPAddress(subnet_props.inspection_iprange.split(',')[1])
    if start >= end:
        message = ('Invalid inspection range specified, inspection_iprange '
                   '"%s" does not come before "%s"' % (start, end))
        LOG.error(message)
        raise FailedValidation(message)


def _validate_no_overlap(subnet_props):
    """Validate the provisioning and inspection ip ranges do not overlap"""
    dhcp_set = netaddr.IPSet(netaddr.IPRange(subnet_props.dhcp_start,
                                             subnet_props.dhcp_end))
    inspection_set = netaddr.IPSet(netaddr.IPRange(
        subnet_props.inspection_iprange.split(',')[0],
        subnet_props.inspection_iprange.split(',')[1]))
    if dhcp_set.intersection(inspection_set):
        message = ('Inspection DHCP range "%s-%s" overlaps provisioning '
                   'DHCP range "%s-%s".' %
                   (subnet_props.inspection_iprange.split(',')[0],
                    subnet_props.inspection_iprange.split(',')[1],
                    subnet_props.dhcp_start, subnet_props.dhcp_end))
        raise FailedValidation(message)


def _validate_interface_exists():
    """Validate the provided local interface exists"""
    if (not CONF.net_config_override
            and CONF.local_interface not in netifaces.interfaces()):
        message = ('Invalid local_interface specified. %s is not available.' %
                   CONF.local_interface)
        LOG.error(message)
        raise FailedValidation(message)


def _validate_no_ip_change():
    """Disallow provisioning interface IP changes

    Changing the provisioning network IP causes a number of issues, so we
    need to disallow it early in the install before configurations start to
    be changed.
    """
    os_net_config_file = '/etc/os-net-config/config.json'
    # Nothing to do if we haven't already installed
    if not os.path.isfile(
            os.path.expanduser(os_net_config_file)):
        return
    with open(os_net_config_file) as f:
        network_config = json.loads(f.read())
    try:
        ctlplane = [i for i in network_config.get('network_config', [])
                    if i['name'] == 'br-ctlplane'][0]
    except IndexError:
        # Nothing to check if br-ctlplane wasn't configured
        return
    existing_ip = ctlplane['addresses'][0]['ip_netmask']
    if existing_ip != CONF.local_ip:
        message = ('Changing the local_ip is not allowed.  Existing IP: '
                   '%s, Configured IP: %s') % (existing_ip,
                                               CONF.local_ip)
        LOG.error(message)
        raise FailedValidation(message)


def _validate_passwords_file():
    """Disallow updates if the passwords file is missing

    If the undercloud was already deployed, the passwords file needs to be
    present so passwords that can't be changed are persisted.  If the file
    is missing it will break the undercloud, so we should fail-fast and let
    the user know about the problem.
    """
    if (os.path.isfile(os.path.expanduser('~/stackrc')) and
            not os.path.isfile(PASSWORD_PATH)):
        message = ('The %s file is missing.  This will cause all service '
                   'passwords to change and break the existing undercloud. ' %
                   PASSWORD_PATH)
        LOG.error(message)
        raise FailedValidation(message)


def _validate_env_files_paths():
    """Verify the non-matching templates path vs env files paths"""
    tht_path = CONF.get('templates', constants.TRIPLEO_HEAT_TEMPLATES)
    roles_file = CONF.get('roles_file', constants.UNDERCLOUD_ROLES_FILE)

    # get the list of jinja templates normally rendered for UC installations
    self.log.debug("Using roles file %s from %s" % (roles_file, tht_path))
    process_templates = os.path.join(tht_path,
                                     'tools/process-templates.py')
    args = ['python', process_templates, '--roles-data',
            roles_file, '--dry-run']
    p = subprocess.Popen(args, cwd=tht_path, stdout=subprocess.PIPE)

    # parse the list for the rendered from j2 file names
    result = p.communicate()[0]
    j2_files_list = []
    for line in result.split("\n"):
        if ((line.startswith('dry run') or line.startswith('jinja2')) and
           line.endswith('.yaml')):
            bname = os.path.basename(line.split(' ')[-1])
            if line.startswith('dry run'):
                j2_files_list.append(bname)
            if line.startswith('jinja2'):
                j2_files_list.append(replace(bname, '.j2', ''))

    # prohibit external env files with the names matching that list entries
    for env_file in CONF['custom_env_files']:
        if (os.path.dirname(os.path.abspath(env_file)) !=
           os.path.abspath(tht_path)):
            env_file_bname = os.path.basename(env_file)
            if env_file_bname in j2_files_list:
                msg = _("External to %s heat environment files "
                        "can not reference j2 processed files, like %s ") %
                        os.path.abspath(tht_path) +
                        os.path.abspath(env_file)
                LOG.error(msg)
                raise FailedValidation(msg)


def _run_yum_clean_all(instack_env):
    args = ['sudo', 'yum', 'clean', 'all']
    LOG.info('Running yum clean all')
    _run_live_command(args, instack_env, 'yum-clean-all')
    LOG.info('yum-clean-all completed successfully')


def _run_yum_update(instack_env):
    args = ['sudo', 'yum', 'update', '-y']
    LOG.info('Running yum update')
    _run_live_command(args, instack_env, 'yum-update')
    LOG.info('yum-update completed successfully')


def check():

    # data = {opt.name: CONF[opt.name] for opt in _opts}
    try:
        # Other validations
        _check_hostname()
        _check_memory()
        _check_sysctl()
        _validate_passwords_file()
        # Heat templates validations
        if CONF.get('custom_env_files'):
            _validate_env_files_paths()
        # Networking validations
        _validate_value_formats()
        for subnet in CONF.subnets:
            s = CONF.get(subnet)
            _validate_in_cidr(s, subnet)
            _validate_dhcp_range(s)
            _validate_inspection_range(s)
            _validate_no_overlap(s)
        _validate_ips()
        _validate_interface_exists()
        _validate_no_ip_change()
    except KeyError as e:
        LOG.error('Key error in configuration: {error}\n'
                  'Value is missing in configuration.'.format(error=e))
        sys.exit(1)
    except FailedValidation as e:
        LOG.error('An error occurred during configuration '
                  'validation, please check your host '
                  'configuration and try again.\nError '
                  'message: {error}'.format(error=e))
        sys.exit(1)
    except RuntimeError as e:
        LOG.error('An error occurred during configuration '
                  'validation, please check your host '
                  'configuration and try again. Error '
                  'message: {error}'.format(error=e))
        sys.exit(1)
