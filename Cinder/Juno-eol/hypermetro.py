# Copyright (c) 2016 Huawei Technologies Co., Ltd.
# All Rights Reserved.
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

from cinder import exception
from cinder.i18n import _, _LI, _LW
from cinder.openstack.common import log as logging
from cinder import utils
from cinder.volume.drivers.huawei import constants
from cinder.volume.drivers.huawei import huawei_utils

LOG = logging.getLogger(__name__)


class HuaweiHyperMetro(object):

    def __init__(self, client, rmt_client, configuration):
        self.client = client
        self.rmt_client = rmt_client
        self.configuration = configuration

    def create_hypermetro(self, local_lun_id, lun_params):
        """Create hypermetro."""

        try:
            # Check remote metro domain is valid.
            domain_id = self._valid_rmt_metro_domain()

            # Get the remote pool info.
            config_pool = self.rmt_client.storage_pools[0]
            remote_pool = self.rmt_client.get_all_pools()
            pool = self.rmt_client.get_pool_info(config_pool, remote_pool)
            if not pool:
                err_msg = _("Remote pool cannot be found.")
                LOG.error(err_msg)
                raise exception.VolumeBackendAPIException(data=err_msg)

            # Create remote lun.
            lun_params['PARENTID'] = pool['ID']
            remotelun_info = self.rmt_client.create_lun(lun_params)
            remote_lun_id = remotelun_info['ID']

            # Get hypermetro domain.
            try:
                self._wait_volume_ready(local_lun_id, True)
                self._wait_volume_ready(remote_lun_id, False)
                hypermetro = self._create_hypermetro_pair(domain_id,
                                                          local_lun_id,
                                                          remote_lun_id)

                LOG.info(_LI("Hypermetro id: %(metro_id)s. "
                             "Remote lun id: %(remote_lun_id)s."),
                         {'metro_id': hypermetro['ID'],
                          'remote_lun_id': remote_lun_id})

                return {'hypermetro_id': hypermetro['ID'],
                        'remote_lun_id': remote_lun_id}
            except exception.VolumeBackendAPIException as err:
                self.rmt_client.delete_lun(remote_lun_id)
                msg = _('Create hypermetro error. %s.') % err
                raise exception.VolumeBackendAPIException(data=msg)
        except exception.VolumeBackendAPIException:
            raise

    def delete_hypermetro(self, volume):
        """Delete hypermetro."""
        metadata = huawei_utils.get_volume_metadata(volume)
        metro_id = metadata['hypermetro_id']
        remote_lun_id = metadata['remote_lun_id']

        # Delete hypermetro.
        if metro_id and self.client.check_hypermetro_exist(metro_id):
            self.check_metro_need_to_stop(metro_id)
            self.client.delete_hypermetro(metro_id)

        # Delete remote lun.
        if remote_lun_id and self.rmt_client.check_lun_exist(remote_lun_id):
            self.rmt_client.delete_lun(remote_lun_id)

    @utils.synchronized('huawei_create_hypermetro_pair', external=True)
    def _create_hypermetro_pair(self, domain_id, lun_id, remote_lun_id):
        """Create a HyperMetroPair."""
        hcp_param = {"DOMAINID": domain_id,
                     "HCRESOURCETYPE": '1',
                     "ISFIRSTSYNC": False,
                     "LOCALOBJID": lun_id,
                     "RECONVERYPOLICY": '1',
                     "REMOTEOBJID": remote_lun_id,
                     "SPEED": '2'}

        return self.client.create_hypermetro(hcp_param)

    def connect_volume_fc(self, volume, connector):
        """Create map between a volume and a host for FC."""
        wwns = connector['wwpns']
        volume_name = huawei_utils.encode_name(volume.id)

        LOG.info(_LI(
            'initialize_connection_fc, initiator: %(wwpns)s,'
            ' volume name: %(volume)s.'),
            {'wwpns': wwns,
             'volume': volume_name})

        metadata = huawei_utils.get_volume_metadata(volume)
        lun_id = metadata['remote_lun_id']

        if lun_id is None:
            lun_id = self.rmt_client.get_lun_id_by_name(volume_name)
        if lun_id is None:
            msg = _("Can't get volume id. Volume name: %s.") % volume_name
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

        original_host_name = connector['host']
        host_name = huawei_utils.encode_host_name(original_host_name)
        host_id = self.client.add_host_with_check(host_name,
                                                  original_host_name)

        # Create hostgroup if not exist.
        host_id = self.rmt_client.add_host_with_check(
            host_name, original_host_name)

        online_wwns_in_host = (
            self.rmt_client.get_host_online_fc_initiators(host_id))
        online_free_wwns = self.rmt_client.get_online_free_wwns()
        fc_initiators_on_array = self.rmt_client.get_fc_initiator_on_array()
        wwns = [i for i in wwns if i in fc_initiators_on_array]
        for wwn in wwns:
            if (wwn not in online_wwns_in_host
                    and wwn not in online_free_wwns):
                wwns_in_host = (
                    self.rmt_client.get_host_fc_initiators(host_id))
                iqns_in_host = (
                    self.rmt_client.get_host_iscsi_initiators(host_id))
                if not (wwns_in_host or iqns_in_host):
                    self.rmt_client.remove_host(host_id)

                msg = _('Can not add FC port to host.')
                LOG.error(msg)
                raise exception.VolumeBackendAPIException(data=msg)

        for wwn in wwns:
            if wwn in online_free_wwns:
                self.rmt_client.add_fc_port_to_host(host_id, wwn)

        (tgt_port_wwns, init_targ_map) = (
            self.rmt_client.get_init_targ_map(wwns))

        # Add host into hostgroup.
        hostgroup_id = self.rmt_client.add_host_to_hostgroup(host_id)
        map_info = self.rmt_client.do_mapping(lun_id,
                                              hostgroup_id,
                                              host_id)
        if not map_info:
            msg = _('Map info is None due to array version '
                    'not supporting hypermetro.')
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

        host_lun_id = self.rmt_client.get_host_lun_id(host_id, lun_id)

        # Return FC properties.
        fc_info = {'driver_volume_type': 'fibre_channel',
                   'data': {'target_lun': int(host_lun_id),
                            'target_discovered': True,
                            'target_wwn': tgt_port_wwns,
                            'volume_id': volume.id,
                            'initiator_target_map': init_targ_map,
                            'map_info': map_info},
                   }

        LOG.info(_LI('Remote return FC info is: %s.'), fc_info)

        return fc_info

    def disconnect_volume_fc(self, volume, connector):
        """Delete map between a volume and a host for FC."""
        wwns = connector['wwpns']
        volume_name = huawei_utils.encode_name(volume.id)
        metadata = huawei_utils.get_volume_metadata(volume)
        lun_id = metadata['remote_lun_id']
        host_name = connector['host']
        left_lunnum = -1
        lungroup_id = None
        view_id = None

        LOG.info(_LI('terminate_connection_fc: volume name: %(volume)s, '
                     'wwpns: %(wwns)s, '
                     'lun_id: %(lunid)s.'),
                 {'volume': volume_name,
                  'wwns': wwns,
                  'lunid': lun_id},)

        host_name = huawei_utils.encode_host_name(host_name)
        hostid = self.rmt_client.get_host_id_by_name(host_name)
        if hostid:
            mapping_view_name = constants.MAPPING_VIEW_PREFIX + hostid
            view_id = self.rmt_client.find_mapping_view(
                mapping_view_name)
            if view_id:
                lungroup_id = self.rmt_client.find_lungroup_from_map(
                    view_id)

        if lun_id and self.rmt_client.check_lun_exist(lun_id):
            if lungroup_id:
                lungroup_ids = self.rmt_client.get_lungroupids_by_lunid(
                    lun_id)
                if lungroup_id in lungroup_ids:
                    self.rmt_client.remove_lun_from_lungroup(
                        lungroup_id, lun_id)
                else:
                    LOG.warning(_LW("Lun is not in lungroup. "
                                    "Lun id: %(lun_id)s, "
                                    "lungroup id: %(lungroup_id)s"),
                                {"lun_id": lun_id,
                                 "lungroup_id": lungroup_id})

        (tgt_port_wwns, init_targ_map) = (
            self.rmt_client.get_init_targ_map(wwns))

        hostid = self.rmt_client.get_host_id_by_name(host_name)
        if hostid:
            mapping_view_name = constants.MAPPING_VIEW_PREFIX + hostid
            view_id = self.rmt_client.find_mapping_view(
                mapping_view_name)
            if view_id:
                lungroup_id = self.rmt_client.find_lungroup_from_map(
                    view_id)
        if lungroup_id:
            left_lunnum = self.rmt_client.get_obj_count_from_lungroup(
                lungroup_id)

        if int(left_lunnum) > 0:
            info = {'driver_volume_type': 'fibre_channel',
                    'data': {}}
        else:
            info = {'driver_volume_type': 'fibre_channel',
                    'data': {'target_wwn': tgt_port_wwns,
                             'initiator_target_map': init_targ_map}, }

        return info

    def _wait_volume_ready(self, lun_id, local=True):
        wait_interval = self.configuration.lun_ready_wait_interval
        client = self.client if local else self.rmt_client

        def _volume_ready():
            result = client.get_lun_info(lun_id)
            if (result['HEALTHSTATUS'] == constants.STATUS_HEALTH
               and result['RUNNINGSTATUS'] == constants.STATUS_VOLUME_READY):
                return True
            return False

        huawei_utils.wait_for_condition(_volume_ready,
                                        wait_interval,
                                        wait_interval * 10)

    def create_consistencygroup(self, group):
        LOG.info(_LI("Create Consistency Group: %(group)s."),
                 {'group': group['id']})
        group_name = huawei_utils.encode_name(group['id'])
        domain_id = self._valid_rmt_metro_domain()
        self.client.create_metrogroup(group_name, group['id'], domain_id)

    def delete_consistencygroup(self, context, group, volumes):
        LOG.info(_LI("Delete Consistency Group: %(group)s."),
                 {'group': group['id']})
        metrogroup_id = self.check_consistencygroup_need_to_stop(group)
        if metrogroup_id:
            # Remove pair from metrogroup.
            for volume in volumes:
                metadata = huawei_utils.get_volume_metadata(volume)
                metro_id = metadata['hypermetro_id']
                if metro_id and self.client.check_hypermetro_exist(metro_id):
                    if self._check_metro_in_cg(metro_id, metrogroup_id):
                        self.client.remove_metro_from_metrogroup(metrogroup_id,
                                                                 metro_id)
                else:
                    err = (_("Hypermetro pair %(id)s doesn't exist on array.")
                           % {'id': metro_id})
                    LOG.warning(err)

            # Delete metrogroup.
            self.client.delete_metrogroup(metrogroup_id)

    def update_consistencygroup(self, context, group,
                                add_volumes, remove_volumes):
        LOG.info(_LI("Update Consistency Group: %(group)s. "
                     "This adds or removes volumes from a CG."),
                 {'group': group['id']})
        model_update = {}
        model_update['status'] = group['status']
        metrogroup_id = self.check_consistencygroup_need_to_stop(group)
        if metrogroup_id:
            # Deal with add volumes to CG
            for volume in add_volumes:
                metadata = huawei_utils.get_volume_metadata(volume)
                metro_id = metadata['hypermetro_id']
                if metro_id and self.client.check_hypermetro_exist(metro_id):
                    if not self._check_metro_in_cg(metro_id, metrogroup_id):
                        self.check_metro_need_to_stop(metro_id)
                        self.client.add_metro_to_metrogroup(metrogroup_id,
                                                            metro_id)
                else:
                    err_msg = _("Hypermetro pair doesn't exist on array.")
                    LOG.error(err_msg)
                    raise exception.VolumeBackendAPIException(data=err_msg)

            # Deal with remove volumes from CG
            for volume in remove_volumes:
                metadata = huawei_utils.get_volume_metadata(volume)
                metro_id = metadata['hypermetro_id']
                if metro_id and self.client.check_hypermetro_exist(metro_id):
                    if self._check_metro_in_cg(metro_id, metrogroup_id):
                        self.check_metro_need_to_stop(metro_id)
                        self.client.remove_metro_from_metrogroup(metrogroup_id,
                                                                 metro_id)
                        self.client.sync_hypermetro(metro_id)
                else:
                    err_msg = _("Hypermetro pair doesn't exist on array.")
                    LOG.error(err_msg)
                    raise exception.VolumeBackendAPIException(data=err_msg)

            new_group_info = self.client.get_metrogroup_by_id(metrogroup_id)
            is_empty = new_group_info["ISEMPTY"]
            if is_empty == 'false':
                self.client.sync_metrogroup(metrogroup_id)

        # if CG not exist on array
        else:
            msg = _("The CG does not exist on array.")
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

    def check_metro_need_to_stop(self, metro_id):
        metro_info = self.client.get_hypermetro_by_id(metro_id)
        metro_health_status = metro_info['HEALTHSTATUS']
        metro_running_status = metro_info['RUNNINGSTATUS']

        if (metro_health_status == constants.HEALTH_NORMAL and
            (metro_running_status == constants.RUNNING_NORMAL or
                metro_running_status == constants.RUNNING_SYNC)):
            self.client.stop_hypermetro(metro_id)

    def check_consistencygroup_need_to_stop(self, group):
        group_name = huawei_utils.encode_name(group['id'])
        metrogroup_id = self.client.get_metrogroup_by_name(group_name)

        if metrogroup_id:
            metrogroup_info = self.client.get_metrogroup_by_id(metrogroup_id)
            health_status = metrogroup_info['HEALTHSTATUS']
            running_status = metrogroup_info['RUNNINGSTATUS']

            if (health_status == constants.HEALTH_NORMAL
                and (running_status == constants.RUNNING_NORMAL
                     or running_status == constants.RUNNING_SYNC)):
                self.client.stop_metrogroup(metrogroup_id)

        return metrogroup_id

    def _check_metro_in_cg(self, metro_id, cg_id):
        metro_info = self.client.get_hypermetro_by_id(metro_id)
        if metro_info['ISINCG'] == 'true' and metro_info['CGID'] == cg_id:
            return True
        return False

    def _valid_rmt_metro_domain(self):
        domain_name = self.rmt_client.metro_domain
        if not domain_name:
            err_msg = _("Hypermetro domain doesn't config.")
            LOG.error(err_msg)
            raise exception.VolumeBackendAPIException(data=err_msg)
        domain_id = self.rmt_client.get_hyper_domain_id(domain_name)
        if not domain_id:
            err_msg = _("Hypermetro domain cannot be found.")
            LOG.error(err_msg)
            raise exception.VolumeBackendAPIException(data=err_msg)

        return domain_id
