#!/usr/bin/env python3
#
# Copyright 2016 Canonical Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import subprocess
import sys
import socket
import uuid

sys.path.append('lib')

import ceph_rgw as ceph
import charms_ceph.utils as ceph_utils
import multisite

from charmhelpers.core.hookenv import (
    relation_get,
    relation_ids,
    related_units,
    config,
    open_port,
    relation_set,
    log,
    DEBUG,
    Hooks, UnregisteredHookError,
    status_set,
    is_leader,
    leader_set,
    leader_get,
)
from charmhelpers.fetch import (
    apt_update,
    apt_install,
    apt_purge,
    add_source,
    filter_installed_packages,
    filter_missing_packages,
)
from charmhelpers.payload.execd import execd_preinstall
from charmhelpers.core.host import (
    cmp_pkgrevno,
    is_container,
    service,
    service_pause,
    service_reload,
    service_restart,
    service_resume,
    service_stop,
)
from charmhelpers.contrib.network.ip import (
    get_relation_ip,
)
from charmhelpers.contrib.openstack.context import ADDRESS_TYPES
from charmhelpers.contrib.openstack.ip import (
    canonical_url,
    PUBLIC, INTERNAL, ADMIN,
)
from charmhelpers.contrib.storage.linux.ceph import (
    send_request_if_needed,
    is_request_complete,
)
from charmhelpers.contrib.openstack.utils import (
    is_unit_paused_set,
    pausable_restart_on_change as restart_on_change,
    series_upgrade_prepare,
    series_upgrade_complete,
)
from charmhelpers.contrib.openstack.ha.utils import (
    generate_ha_relation_data,
)
from utils import (
    assess_status,
    disable_unused_apache_sites,
    listen_port,
    multisite_deployment,
    pause_unit_helper,
    ready_for_service,
    register_configs,
    request_per_unit_key,
    restart_map,
    restart_nonce_changed,
    resume_unit_helper,
    service_name,
    services,
    setup_ipv6,
    systemd_based_radosgw,
)
from charmhelpers.contrib.charmsupport import nrpe
from charmhelpers.contrib.hardening.harden import harden

from charmhelpers.contrib.openstack.cert_utils import (
    get_certificate_request,
    process_certificates,
)

hooks = Hooks()
CONFIGS = register_configs()

PACKAGES = [
    'haproxy',
    'ntp',
    'radosgw',
    'apache2'
]

APACHE_PACKAGES = [
    'libapache2-mod-fastcgi',
]

MULTISITE_SYSTEM_USER = 'multisite-sync'


def upgrade_available():
    """Check for upgrade for ceph

    :returns: whether an upgrade is available
    :rtype: boolean
    """
    c = config()
    old_version = ceph_utils.resolve_ceph_version(c.previous('source') or
                                                  'distro')
    new_version = ceph_utils.resolve_ceph_version(c.get('source'))
    if (old_version in ceph_utils.UPGRADE_PATHS and
            new_version == ceph_utils.UPGRADE_PATHS[old_version]):
        return True
    return False


def install_packages():
    c = config()
    if c.changed('source') or c.changed('key'):
        add_source(c.get('source'), c.get('key'))
        apt_update(fatal=True)

    if is_container():
        PACKAGES.remove('ntp')

    # NOTE: just use full package list if we're in an upgrade
    #       config-changed execution
    pkgs = (
        PACKAGES if upgrade_available() else
        filter_installed_packages(PACKAGES)
    )
    if pkgs:
        status_set('maintenance', 'Installing radosgw packages')
        if ('apache2' in pkgs):
            # NOTE(lourot): Apache's default config makes it listen on port 80,
            # which will prevent HAProxy from listening on that same port. We
            # use Apache in this setup however for SSL (different port). We
            # need to let Apache free port 80 before we can install HAProxy
            # otherwise HAProxy will crash. See lp:1904411
            log('Installing Apache')
            apt_install(['apache2'], fatal=True)
            disable_unused_apache_sites()
        apt_install(pkgs, fatal=True)

    pkgs = filter_missing_packages(APACHE_PACKAGES)
    if pkgs:
        apt_purge(pkgs)


@hooks.hook('install.real')
@harden()
def install():
    status_set('maintenance', 'Executing pre-install')
    execd_preinstall()
    install_packages()
    # hold the service down until we have keys from ceph
    log('Disable service "{}" until we have keys for it.'
        .format(service_name()), level=DEBUG)
    service_pause(service_name())
    if not os.path.exists('/etc/ceph'):
        os.makedirs('/etc/ceph')
    if is_leader():
        leader_set(namespace_tenants=config('namespace-tenants'))


@hooks.hook('object-store-relation-joined')
def object_store_joined(relation_id=None):
    relation_data = {
        'swift-url':
        "{}:{}".format(canonical_url(CONFIGS, INTERNAL), listen_port())
    }
    relation_set(relation_id=relation_id, relation_settings=relation_data)


@hooks.hook('upgrade-charm.real')
def upgrade_charm():
    if is_leader() and not leader_get('namespace_tenants') == 'True':
        leader_set(namespace_tenants=False)


@hooks.hook('config-changed')
@harden()
def config_changed():
    @restart_on_change(restart_map())
    def _config_changed():
        # if we are paused, delay doing any config changed hooks.
        # It is forced on the resume.
        if is_unit_paused_set():
            log("Unit is pause or upgrading. Skipping config_changed", "WARN")
            return

        install_packages()

        if config('prefer-ipv6'):
            status_set('maintenance', 'configuring ipv6')
            setup_ipv6()

        for r_id in relation_ids('identity-service'):
            identity_changed(relid=r_id)

        for r_id in relation_ids('cluster'):
            cluster_joined(rid=r_id)

        # NOTE(jamespage): Re-exec mon relation for any changes to
        #                  enable ceph pool permissions restrictions
        for r_id in relation_ids('mon'):
            for unit in related_units(r_id):
                mon_relation(r_id, unit)

        # Re-trigger hacluster relations to switch to ifaceless
        # vip configuration
        for r_id in relation_ids('ha'):
            ha_relation_joined(r_id)

        # Refire certificates relations for VIP changes
        for r_id in relation_ids('certificates'):
            certs_joined(r_id)

        # Refire object-store relations for VIP/port changes
        for r_id in relation_ids('object-store'):
            object_store_joined(r_id)

        process_multisite_relations()

        CONFIGS.write_all()
        configure_https()

        update_nrpe_config()

        open_port(port=listen_port())
    _config_changed()


@hooks.hook('mon-relation-departed',
            'mon-relation-changed')
def mon_relation(rid=None, unit=None):
    @restart_on_change(restart_map())
    def _mon_relation():
        key_name = 'rgw.{}'.format(socket.gethostname())
        if request_per_unit_key():
            relation_set(relation_id=rid,
                         key_name=key_name)
        try:
            rq = ceph.get_create_rgw_pools_rq(
                prefix=config('zone') or config('pool-prefix'))
        except ValueError as e:
            # The end user has most likely provided a invalid value for
            # a configuration option. Just log the traceback here, the
            # end user will be notified by assess_status() called at
            # the end of the hook execution.
            log('Caught ValueError, invalid value provided for '
                'configuration?: "{}"'.format(str(e)),
                level=DEBUG)
            return

        if is_request_complete(rq, relation='mon'):
            log('Broker request complete', level=DEBUG)
            CONFIGS.write_all()
            # New style per unit keys
            key = relation_get(attribute='{}_key'.format(key_name),
                               rid=rid, unit=unit)
            if not key:
                # Fallback to old style global key
                key = relation_get(attribute='radosgw_key',
                                   rid=rid, unit=unit)
                key_name = None

            if key:
                new_keyring = ceph.import_radosgw_key(key,
                                                      name=key_name)
                # NOTE(jamespage):
                # Deal with switch from radosgw init script to
                # systemd named units for radosgw instances by
                # stopping and disabling the radosgw unit
                if systemd_based_radosgw():
                    service_stop('radosgw')
                    service('disable', 'radosgw')
                    # Update the nrpe config. If we wait for the below
                    # to be called elsewhere, there exists a period
                    # where nagios will report the radosgw service as
                    # down, and also not be monitoring the per
                    # host services.
                    update_nrpe_config(checks_to_remove=['radosgw'])

                # NOTE(jamespage):
                # Multi-site deployments need to defer restart as the
                # zone is not created until the master relation is
                # joined; restarting here will cause a restart burst
                # in systemd and stop the process restarting once
                # zone configuration is complete.
                if (not is_unit_paused_set() and
                        new_keyring and
                        not multisite_deployment()):
                    log('Resume service "{}" as we now have keys for it.'
                        .format(service_name()), level=DEBUG)
                    service_resume(service_name())

            if multisite_deployment():
                process_multisite_relations()
            elif is_leader() and 'mon' in CONFIGS.complete_contexts():
                # In a non multi-site deployment create the
                # zone using the default zonegroup and restart the service
                internal_url = '{}:{}'.format(
                    canonical_url(CONFIGS, INTERNAL),
                    listen_port(),
                )
                endpoints = [internal_url]
                zonegroup = 'default'
                zone = config('zone')
                existing_zones = multisite.list_zones()
                log('Existing zones {}'.format(existing_zones), level=DEBUG)
                if zone not in existing_zones:
                    log("Zone '{}' doesn't exist, creating".format(zone))
                    try:
                        multisite.create_zone(zone,
                                              endpoints=endpoints,
                                              default=True, master=True,
                                              zonegroup=zonegroup)
                    except subprocess.CalledProcessError as e:
                        if 'File exists' in e.stderr.decode('UTF-8'):
                            # NOTE(lourot): may have been created in the
                            # background by the Rados Gateway daemon, see
                            # lp:1856106
                            log("Zone '{}' existed already after all".format(
                                zone))
                        else:
                            raise

                    existing_zones = multisite.list_zones()
                    log('Existing zones {}'.format(existing_zones),
                        level=DEBUG)
                    if zone not in existing_zones:
                        raise RuntimeError("Could not create zone '{}'".format(
                            zone))

                    service_restart(service_name())
        else:
            send_request_if_needed(rq, relation='mon')
    _mon_relation()


@hooks.hook('gateway-relation-joined')
def gateway_relation():
    relation_set(hostname=get_relation_ip('gateway-relation'),
                 port=listen_port())


@hooks.hook('identity-service-relation-joined')
def identity_joined(relid=None):
    if cmp_pkgrevno('radosgw', '0.55') < 0:
        log('Integration with keystone requires ceph >= 0.55')
        sys.exit(1)

    port = listen_port()
    admin_url = '%s:%i/swift' % (canonical_url(CONFIGS, ADMIN), port)
    if leader_get('namespace_tenants') == 'True':
        internal_url = '%s:%s/swift/v1/AUTH_$(project_id)s' % \
            (canonical_url(CONFIGS, INTERNAL), port)
        public_url = '%s:%s/swift/v1/AUTH_$(project_id)s' % \
            (canonical_url(CONFIGS, PUBLIC), port)
    else:
        internal_url = '%s:%s/swift/v1' % \
            (canonical_url(CONFIGS, INTERNAL), port)
        public_url = '%s:%s/swift/v1' % \
            (canonical_url(CONFIGS, PUBLIC), port)
    roles = [x for x in [config('operator-roles'), config('admin-roles')] if x]
    requested_roles = ''
    if roles:
        requested_roles = ','.join(roles) if len(roles) > 1 else roles[0]
    relation_set(swift_service='swift',
                 swift_region=config('region'),
                 swift_public_url=public_url,
                 swift_internal_url=internal_url,
                 swift_admin_url=admin_url,
                 requested_roles=requested_roles,
                 relation_id=relid)
    if cmp_pkgrevno('radosgw', '12.2') >= 0:
        relation_set(s3_service='s3',
                     s3_region=config('region'),
                     s3_public_url='{}:{}/'.format(
                         canonical_url(CONFIGS, PUBLIC), port),
                     s3_internal_url='{}:{}/'.format(
                         canonical_url(CONFIGS, INTERNAL), port),
                     s3_admin_url='{}:{}/'.format(
                         canonical_url(CONFIGS, ADMIN), port),
                     relation_id=relid)


@hooks.hook('identity-service-relation-changed')
def identity_changed(relid=None):
    @restart_on_change(restart_map())
    def _identity_changed():
        identity_joined(relid)
        CONFIGS.write_all()
    _identity_changed()


@hooks.hook('cluster-relation-joined')
def cluster_joined(rid=None):
    @restart_on_change(restart_map())
    def _cluster_joined():
        settings = {}

        for addr_type in ADDRESS_TYPES:
            address = get_relation_ip(
                addr_type,
                cidr_network=config('os-{}-network'.format(addr_type)))
            if address:
                settings['{}-address'.format(addr_type)] = address

        settings['private-address'] = get_relation_ip('cluster')

        relation_set(relation_id=rid, relation_settings=settings)
    _cluster_joined()


@hooks.hook('cluster-relation-changed')
def cluster_changed():
    @restart_on_change(restart_map())
    def _cluster_changed():
        CONFIGS.write_all()
        for r_id in relation_ids('identity-service'):
            identity_joined(relid=r_id)
        for r_id in relation_ids('certificates'):
            for unit in related_units(r_id):
                certs_changed(r_id, unit)
    _cluster_changed()


@hooks.hook('ha-relation-joined')
def ha_relation_joined(relation_id=None):
    settings = generate_ha_relation_data('cephrg')
    relation_set(relation_id=relation_id, **settings)


@hooks.hook('ha-relation-changed')
def ha_relation_changed():
    clustered = relation_get('clustered')
    if clustered:
        log('Cluster configured, notifying other services and'
            'updating keystone endpoint configuration')
        # Tell all related services to start using
        # the VIP instead
        for r_id in relation_ids('identity-service'):
            identity_joined(relid=r_id)


@hooks.hook('nrpe-external-master-relation-joined',
            'nrpe-external-master-relation-changed')
def update_nrpe_config(checks_to_remove=None):
    """
    Update the checks for the nagios plugin.

    :param checks_to_remove: list of short names of nrpe checks to
        remove. For example, pass ['radosgw'] to remove the check for
        the default systemd radosgw service, to make way for per host
        services.
    :type checks_to_remove: list

    """
    # python-dbus is used by check_upstart_job
    apt_install('python-dbus')
    hostname = nrpe.get_nagios_hostname()
    current_unit = nrpe.get_nagios_unit_name()
    nrpe_setup = nrpe.NRPE(hostname=hostname)
    nrpe.copy_nrpe_checks()
    if checks_to_remove is not None:
        log("Removing the following nrpe checks: {}".format(checks_to_remove),
            level=DEBUG)
        for svc in checks_to_remove:
            nrpe_setup.remove_check(shortname=svc)
    nrpe.add_init_service_checks(nrpe_setup, services(), current_unit)
    nrpe.add_haproxy_checks(nrpe_setup, current_unit)
    nrpe_setup.write()


def configure_https():
    '''Enables SSL API Apache config if appropriate and kicks
    identity-service and image-service with any required
    updates
    '''
    CONFIGS.write_all()
    if 'https' in CONFIGS.complete_contexts():
        cmd = ['a2ensite', 'openstack_https_frontend']
        subprocess.check_call(cmd)
    else:
        cmd = ['a2dissite', 'openstack_https_frontend']
        try:
            subprocess.check_call(cmd)
        except subprocess.CalledProcessError:
            # The site is not yet enabled or
            # https is not configured
            pass

    # TODO: improve this by checking if local CN certs are available
    # first then checking reload status (see LP #1433114).
    if not is_unit_paused_set():
        service_reload('apache2', restart_on_failure=True)


@hooks.hook('update-status')
@harden()
def update_status():
    log('Updating status.')


@hooks.hook('pre-series-upgrade')
def pre_series_upgrade():
    log("Running prepare series upgrade hook", "INFO")
    series_upgrade_prepare(
        pause_unit_helper, CONFIGS)


@hooks.hook('post-series-upgrade')
def post_series_upgrade():
    log("Running complete series upgrade hook", "INFO")
    series_upgrade_complete(
        resume_unit_helper, CONFIGS)


@hooks.hook('certificates-relation-joined')
def certs_joined(relation_id=None):
    relation_set(
        relation_id=relation_id,
        relation_settings=get_certificate_request())


@hooks.hook('certificates-relation-changed')
def certs_changed(relation_id=None, unit=None):
    @restart_on_change(restart_map(), stopstart=True)
    def _certs_changed():
        process_certificates('ceph-radosgw', relation_id, unit)
        configure_https()
    _certs_changed()


@hooks.hook('master-relation-joined')
def master_relation_joined(relation_id=None):
    if not ready_for_service(legacy=False):
        log('unit not ready, deferring multisite configuration')
        return

    internal_url = '{}:{}'.format(
        canonical_url(CONFIGS, INTERNAL),
        listen_port(),
    )
    endpoints = [internal_url]
    realm = config('realm')
    zonegroup = config('zonegroup')
    zone = config('zone')
    access_key = leader_get('access_key')
    secret = leader_get('secret')

    if not all((realm, zonegroup, zone)):
        return

    relation_set(relation_id=relation_id,
                 realm=realm,
                 zonegroup=zonegroup,
                 url=endpoints[0],
                 access_key=access_key,
                 secret=secret)

    if not is_leader():
        return

    if not leader_get('restart_nonce'):
        # NOTE(jamespage):
        # This is an ugly kludge to force creation of the required data
        # items in the .rgw.root pool prior to the radosgw process being
        # started; radosgw-admin does not currently have a way of doing
        # this operation but a period update will force it to be created.
        multisite.update_period(fatal=False)

    mutation = False

    if realm not in multisite.list_realms():
        multisite.create_realm(realm, default=True)
        mutation = True

    if zonegroup not in multisite.list_zonegroups():
        multisite.create_zonegroup(zonegroup,
                                   endpoints=endpoints,
                                   default=True, master=True,
                                   realm=realm)
        mutation = True

    if zone not in multisite.list_zones():
        multisite.create_zone(zone,
                              endpoints=endpoints,
                              default=True, master=True,
                              zonegroup=zonegroup)
        mutation = True

    if MULTISITE_SYSTEM_USER not in multisite.list_users():
        access_key, secret = multisite.create_system_user(
            MULTISITE_SYSTEM_USER
        )
        multisite.modify_zone(zone,
                              access_key=access_key,
                              secret=secret)
        leader_set(access_key=access_key,
                   secret=secret)
        mutation = True

    if mutation:
        multisite.update_period()
        service_restart(service_name())
        leader_set(restart_nonce=str(uuid.uuid4()))

    relation_set(relation_id=relation_id,
                 access_key=access_key,
                 secret=secret)


@hooks.hook('slave-relation-changed')
def slave_relation_changed(relation_id=None, unit=None):
    if not is_leader():
        return
    if not ready_for_service(legacy=False):
        log('unit not ready, deferring multisite configuration')
        return

    master_data = relation_get(rid=relation_id, unit=unit)
    if not all((master_data.get('realm'),
                master_data.get('zonegroup'),
                master_data.get('access_key'),
                master_data.get('secret'),
                master_data.get('url'))):
        log("Defer processing until master RGW has provided required data")
        return

    internal_url = '{}:{}'.format(
        canonical_url(CONFIGS, INTERNAL),
        listen_port(),
    )
    endpoints = [internal_url]

    realm = config('realm')
    zonegroup = config('zonegroup')
    zone = config('zone')

    if (realm, zonegroup) != (master_data['realm'],
                              master_data['zonegroup']):
        log("Mismatched configuration so stop multi-site configuration now")
        return

    if not leader_get('restart_nonce'):
        # NOTE(jamespage):
        # This is an ugly kludge to force creation of the required data
        # items in the .rgw.root pool prior to the radosgw process being
        # started; radosgw-admin does not currently have a way of doing
        # this operation but a period update will force it to be created.
        multisite.update_period(fatal=False)

    mutation = False

    if realm not in multisite.list_realms():
        multisite.pull_realm(url=master_data['url'],
                             access_key=master_data['access_key'],
                             secret=master_data['secret'])
        multisite.pull_period(url=master_data['url'],
                              access_key=master_data['access_key'],
                              secret=master_data['secret'])
        multisite.set_default_realm(realm)
        mutation = True

    if zone not in multisite.list_zones():
        multisite.create_zone(zone,
                              endpoints=endpoints,
                              default=False, master=False,
                              zonegroup=zonegroup,
                              access_key=master_data['access_key'],
                              secret=master_data['secret'])
        mutation = True

    if mutation:
        multisite.update_period()
        service_restart(service_name())
        leader_set(restart_nonce=str(uuid.uuid4()))


@hooks.hook('leader-settings-changed')
def leader_settings_changed():
    # NOTE: leader unit will only ever set leader storage
    #       data when multi-site realm, zonegroup, zone or user
    #       data has been created/changed - trigger restarts
    #       of rgw services.
    if restart_nonce_changed(leader_get('restart_nonce')):
        service_restart(service_name())
    if not is_leader():
        for r_id in relation_ids('master'):
            master_relation_joined(r_id)


def process_multisite_relations():
    """Re-trigger any pending master/slave relations"""
    for r_id in relation_ids('master'):
        master_relation_joined(r_id)
    for r_id in relation_ids('slave'):
        for unit in related_units(r_id):
            slave_relation_changed(r_id, unit)


if __name__ == '__main__':
    try:
        hooks.execute(sys.argv)
    except UnregisteredHookError as e:
        log('Unknown hook {} - skipping.'.format(e))
    assess_status(CONFIGS)
