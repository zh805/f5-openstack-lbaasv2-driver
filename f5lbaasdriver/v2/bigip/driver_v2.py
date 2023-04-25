# coding=utf-8
u"""F5 Networks® LBaaSv2 Driver Implementation."""
# Copyright 2016 F5 Networks Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

import os
import random
import sys
from time import sleep

from oslo_db import exception as db_exc
from oslo_log import helpers as log_helpers
from oslo_log import log as logging
from oslo_utils import importutils

from neutron.callbacks import events
from neutron.callbacks import registry
from neutron.callbacks import resources
from neutron.plugins.common import constants as plugin_constants
from neutron_lib.api.definitions import portbindings
from neutron_lib.plugins import constants as pg_const
from neutron_lib.plugins import directory

from neutron_lbaas import agent_scheduler
from neutron_lbaas.db.loadbalancer import models
from neutron_lbaas.services.loadbalancer import data_models

from neutron_lbaas_inventory.db.inventory_db import InventoryDbPlugin

from f5lbaasdriver.v2.bigip import agent_rpc
from f5lbaasdriver.v2.bigip import config
from f5lbaasdriver.v2.bigip import device_scheduler
from f5lbaasdriver.v2.bigip import exceptions as f5_exc
from f5lbaasdriver.v2.bigip import neutron_client
from f5lbaasdriver.v2.bigip import plugin_rpc
from f5lbaasdriver.v2.bigip import validator
# from neutron.api.v2 import attributes
from neutron_lib import constants as n_const

cfg = config.cfg
LOG = logging.getLogger(__name__)


class F5NoAttachedLoadbalancerException(f5_exc.F5LBaaSv2DriverException):
    """Exception thrown when an LBaaSv2 object has not parent Loadbalancer."""

    message = "Entity has no associated loadbalancer"

    def __str__(self):
        return self.message


class F5DriverV2(object):
    u"""F5 Networks® LBaaSv2 Driver."""

    def __init__(self, plugin=None, env=None):
        """Driver initialization."""
        if not plugin:
            LOG.error('Required LBaaS Driver and Core Driver Missing')
            sys.exit(1)

        self.plugin = plugin
        # NOTE(qzhao): Append L3 plugin to LBaaSv2 DB plugin
        if not getattr(self.plugin.db, '_l3_plugin', None):
            self.plugin.db._l3_plugin = directory.get_plugin(pg_const.L3)

        self.env = env
        self.inventory_plugin = InventoryDbPlugin()

        self.loadbalancer = LoadBalancerManager(self)
        self.listener = ListenerManager(self)
        self.pool = PoolManager(self)
        self.member = MemberManager(self)
        self.healthmonitor = HealthMonitorManager(self)
        self.l7policy = L7PolicyManager(self)
        self.l7rule = L7RuleManager(self)
        self.acl_group = ACLGroupManager(self)

        # what scheduler to use for pool selection
        self.agent_scheduler = importutils.import_object(
            cfg.CONF.loadbalancer_agent_scheduler)
        self.device_scheduler = importutils.import_object(
            cfg.CONF.loadbalancer_device_scheduler)
        self.device_scheduler.driver = self

        self.service_builder = importutils.import_object(
            cfg.CONF.f5_loadbalancer_service_builder_v2, self)

        self.agent_rpc = agent_rpc.LBaaSv2AgentRPC(self)
        self.plugin_rpc = plugin_rpc.LBaaSv2PluginCallbacksRPC(self)

        self.q_client = \
            neutron_client.F5NetworksNeutronClient(self.plugin)

        # add this agent RPC to the neutron agent scheduler
        # mixins agent_notifiers dictionary for it's env
        self.plugin.agent_notifiers.update(
            {n_const.AGENT_TYPE_LOADBALANCER: self.agent_rpc})

        registry.subscribe(self._bindRegistryCallback(),
                           resources.PROCESS,
                           events.AFTER_INIT)

    def _bindRegistryCallback(self):
        # Defines a callback function with name tied to driver env. Need to
        # enusre unique name, as registry callback manager references callback
        # functions by name.
        def post_fork_callback(resources, event, trigger):
            LOG.debug("F5DriverV2 with env %s received post neutron child "
                      "fork notification pid(%d) print trigger(%s)" % (
                          self.env, os.getpid(), trigger))
            self.plugin_rpc.create_rpc_listener()

        post_fork_callback.__name__ += '_' + str(self.env)
        return post_fork_callback


class EntityManager(object):
    '''Parent for all managers defined in this module.'''

    def __init__(self, driver):
        self.driver = driver
        self.model = None

    def _log_entity(self, entity):
        LOG.debug("Log the entity: %s", entity.to_api_dict())

    def _handle_entity_error(self, context, id, **kwargs):
        status = kwargs.get("status", plugin_constants.ERROR)
        lb_id = kwargs.get("loadbalancer_id", None)
        if lb_id:
            self.driver.plugin.db.update_status(context, models.LoadBalancer,
                                                lb_id, status)
        if self.model:
            self.driver.plugin.db.update_status(context, self.model, id,
                                                status)

    def _call_rpc(self, context, loadbalancer, entity, api_dict,
                  rpc_method, **kwargs):
        '''Perform operations common to create and delete for managers.'''

        try:
            agent_host, service = self._setup_crud(
                context, loadbalancer, entity, **kwargs)
            rpc_callable = getattr(self.driver.agent_rpc, rpc_method)

            the_port = kwargs.get("the_port_id", None)
            LOG.info(the_port)
            if the_port:
                LOG.info('the_port is not None')
                rpc_callable(
                    context, api_dict, service,
                    agent_host, the_port_id=the_port
                )
            else:
                LOG.info('the_port is None')
                rpc_callable(context, api_dict, service, agent_host)
        except Exception as e:
            LOG.error("Exception: %s: %s" % (rpc_method, e))
            raise e

    def _setup_crud(self, context, loadbalancer, entity, **kwargs):
        '''Setup CRUD operations for managers to make calls to agent.

        :param context: auth context for performing CRUD operation
        :param entity: neutron lbaas entity -- target of the CRUD operation
        :returns: tuple -- (agent object, service dict)
        :raises: F5NoAttachedLoadbalancerException
        '''

        if entity.attached_to_loadbalancer() and loadbalancer:
            agent, device = self._schedule_agent_and_device(
                context, loadbalancer, entity, **kwargs)
            service = self._create_service(context, loadbalancer, agent,
                                           entity, **kwargs)
            service["device"] = device
            return agent['host'], service

        raise F5NoAttachedLoadbalancerException()

    def _schedule_agent_and_device(self, context, loadbalancer,
                                   entity=None, **kwargs):
        '''Schedule agent --used for most managers.

        :param context: auth context for performing crud operation
        :returns: agent object
        '''

        LAB = agent_scheduler.LoadbalancerAgentBinding

        # If LB is already hosted on an agent, return this agent and device
        result = self.driver.plugin.db.get_agent_hosting_loadbalancer(
            context, loadbalancer.id)

        if result:
            agent = result["agent"]
            if not agent["alive"] or not agent["admin_state_up"]:
                # Agent is not alive or is disabled. Attempt to
                # reschedule this loadbalancer to a new agent.
                LOG.info("Reschedule loadbalancer %s", loadbalancer.id)
                agent = self.driver.agent_scheduler.schedule(
                    self.driver.plugin,
                    context,
                    loadbalancer,
                    self.driver.env
                )
                # Update binding table
                with context.session.begin(subtransactions=True):
                    query = context.session.query(LAB)
                    binding = query.get(loadbalancer.id)
                    binding.agent_id = agent["id"]
                LOG.info("Loadbalancer %s is rescheduled to agent %s",
                         loadbalancer.id, agent.id)

            # Load device info and return
            device_id = result["device_id"]
            device = self.driver.device_scheduler.load_device(context,
                                                              device_id)
            if device and device["admin_state_up"]:
                LOG.debug("choose active device here %s ", device_id)
                return agent, device

            if device and not device["admin_state_up"]:
                name = loadbalancer.name
                if (
                    name
                    and cfg.CONF.special_lb_name_prefix
                    and cfg.CONF.special_lb_name_prefix in name
                ):
                    id_prefix = device_id[:8]
                    match_regex = cfg.CONF.special_lb_name_prefix + id_prefix
                    if match_regex in name:
                        LOG.debug("choose inactive device here %s ", device_id)
                        return agent, device

            if not device:
                raise device_scheduler.LbaasDeviceDisappeared(
                    loadbalancer_id=loadbalancer.id,
                    device_id=device_id)

            if not device["admin_state_up"]:
                raise device_scheduler.LbaasDeviceDisabled(
                    loadbalancer_id=loadbalancer.id,
                    device_id=device_id)

        # If no binding
        if loadbalancer.provisioning_status == n_const.PENDING_CREATE:
            # LB is being created. Let's go.
            pass
        elif loadbalancer.provisioning_status == n_const.PENDING_DELETE:
            # LB is being deleted. LB db and binding db are inconsistent.
            # However, we can silently delete LB from db. It only happens
            # in development phase.
            LOG.info("No binding information for loadbalancer %s",
                     loadbalancer.id)
            return None, None
        else:
            # LB db and binding db are inconsistent. It only happens in
            # development phase.
            raise Exception("No binding information for loadbalancer %s",
                            loadbalancer.id)

        # Schedule agent and device for new LB
        agent = self.driver.agent_scheduler.schedule(
            self.driver.plugin,
            context,
            loadbalancer,
            self.driver.env
        )

        binding = LAB()
        binding.loadbalancer_id = loadbalancer.id
        binding.agent = agent
        binding.device_id = "unknown"

        max_wait = 30
        wait = 0
        attempt = 1
        while wait <= max_wait:
            try:
                with context.session.begin(subtransactions=True):
                    # Insert a new row without device assigned
                    context.session.add(binding)
                    context.session.flush()

                    # Lock all rows who do not have devices assigned.
                    query = context.session.query(LAB).populate_existing(
                        ).with_for_update().filter_by(device_id="unknown")

                    # Fetch data from query object, in order to ensure it
                    # is executed against db
                    b = binding
                    for x in query:
                        if x.loadbalancer_id == binding.loadbalancer_id:
                            b = x
                            break

                    # Schedule device
                    device = self.driver.device_scheduler.schedule(
                        self.driver.plugin, context, loadbalancer
                    )
                    # Update the new row's device_id
                    b.device_id = device["id"]
                    context.session.add(b)
                break
            except db_exc.DBDeadlock as ex:
                # NOTE(qzhao):  A request who does not get the row lock
                # may encounter deadlock error. It can retry, and should
                # give up eventually, if it can not acquire the lock.
                LOG.info("Attempt %s DB deadlock: %s", attempt, ex)
                interval = random.uniform(0, 1.0)
                if wait + interval <= max_wait:
                    # Wait up to 1 second
                    wait += interval
                    attempt += 1
                    sleep(interval)
                else:
                    raise device_scheduler.DeviceSchedulerBusy(
                        loadbalancer_id=loadbalancer.id)

        LOG.info("LB %s is scheduled to agent %s device %s",
                 loadbalancer.id, agent["id"], device["id"])

        return agent, device

    def _create_service(self, context, loadbalancer, agent,
                        entity=None, **kwargs):
        '''build service--used for most managers.

        :param context: auth context for performing crud operation
        :returns: service dict
        '''

        service = self.driver.service_builder.build(
            context, loadbalancer, agent, **kwargs)
        return service

    @log_helpers.log_method_call
    def _append_listeners(self, context, service, listener):

        if not listener:
            return

        def get_db_listener():
            if cfg.CONF.f5_driver_perf_mode == 3:
                return listener
            else:
                return self.driver.plugin.db.get_listener(
                    context, listener.id)

        db_listener = get_db_listener()
        listener_dict = db_listener.to_dict(
            loadbalancer=False,
            default_pool=False,
            l7_policies=False
        )

        # Listener may have l7policies or default pool
        listener_dict['l7_policies'] = \
            [{'id': l7_policy.id} for l7_policy in listener.l7_policies]
        if listener.default_pool:
            listener_dict['default_pool_id'] = listener.default_pool.id

        LOG.debug("append listener %s", listener_dict)
        service['listeners'].append(listener_dict)

    @log_helpers.log_method_call
    def _append_pools_monitors(self, context, service, pool):

        if not pool:
            return

        def get_db_pool():
            if cfg.CONF.f5_driver_perf_mode == 3:
                return pool
            else:
                return self.driver.plugin.db.get_pool(
                    context, pool.id)

        db_pool = get_db_pool()

        pool_dict = db_pool.to_dict(
            healthmonitor=False,
            listener=False,
            listeners=False,
            loadbalancer=False,
            l7_policies=False,
            members=False,
            session_persistence=False
        )

        pool_dict['members'] = [{'id': member.id} for member in pool.members]
        pool_dict['l7_policies'] = [
            {'id': l7_policy.id} for l7_policy in pool.l7_policies]

        if pool.session_persistence:
            pool_dict['session_persistence'] = (
                pool.session_persistence.to_api_dict()
            )

        LOG.debug("append pool %s", pool_dict)
        service['pools'].append(pool_dict)

        if not pool.healthmonitor:
            return

        def get_db_healthmonitor():
            if cfg.CONF.f5_driver_perf_mode == 3:
                return pool.healthmonitor
            else:
                return self.driver.plugin.db.get_healthmonitor(
                    context,
                    pool.healthmonitor.id
                )

        healthmonitor = get_db_healthmonitor()
        healthmonitor_dict = healthmonitor.to_dict(pool=False)
        healthmonitor_dict['pool_id'] = pool.id

        LOG.debug("append healthmonitor_dict %s", healthmonitor_dict)
        service['healthmonitors'].append(healthmonitor_dict)


class LoadBalancerManager(EntityManager):
    """LoadBalancerManager class handles Neutron LBaaS CRUD."""

    def __init__(self, driver):
        super(LoadBalancerManager, self).__init__(driver)
        self.model = models.LoadBalancer
        self.validators = [
            validator.FlavorValidator(),
            validator.SnatIPValidator(driver)
        ]

    @log_helpers.log_method_call
    def create(self, context, loadbalancer):
        """Create a loadbalancer."""

        self._log_entity(loadbalancer)

        driver = self.driver
        try:
            for v in self.validators:
                v.validate_create(context, loadbalancer)

            service = {}
            agent, device = self._schedule_agent_and_device(
                context, loadbalancer)
            agent_host = agent['host']
            agent_config = agent.get('configurations', {})
            LOG.debug("agent configurations: %s" % agent_config)

            if agent in context.session:
                LOG.info('inside here')
                context.session.expire(agent, ['heartbeat_timestamp'])
                LOG.info(agent)

            # Update the port for the VIP to show ownership by this driver
            port_data = {
                'admin_state_up': True,
                'device_owner': 'network:f5lbaasv2',
                'status': n_const.PORT_STATUS_ACTIVE
            }
            port_data[portbindings.HOST_ID] = agent_host
            port_data[portbindings.VNIC_TYPE] = "baremetal"

            port_data[portbindings.PROFILE] = {}

            vip_masq_mac = device['device_info'].get('masquerade_mac')
            if not vip_masq_mac:
                LOG.error(
                    "Can not find masquerade_mac in device %s, when"
                    " creating loadbalancer %s." % (
                        device, loadbalancer
                    )
                )

            # llinfo is a list of dict type
            llinfo = device.get('local_link_information', None)

            if llinfo:
                link_info = llinfo[0]
            else:
                link_info = dict()
                llinfo = [link_info]

            link_info.update({"lb_mac": vip_masq_mac})

            port_data[portbindings.PROFILE] = {
                "local_link_information": llinfo
            }

            driver.plugin.db._core_plugin.update_port(
                context,
                loadbalancer.vip_port_id,
                {'port': port_data}
            )

            # NOTE(qzhao): Vlan id might be assigned after updating vip
            # port. Need to build service payload after updating port.
            service = self._create_service(context, loadbalancer, agent)
            service["device"] = device

            driver.agent_rpc.create_loadbalancer(
                context, loadbalancer.to_api_dict(), service, agent_host)
        except Exception as e:
            LOG.error("Exception: loadbalancer create: %s" % e.message)
            self._handle_entity_error(context, loadbalancer.id)
            raise e

    @log_helpers.log_method_call
    def update(self, context, old_loadbalancer, loadbalancer):
        """Update a loadbalancer."""

        self._log_entity(old_loadbalancer)
        self._log_entity(loadbalancer)

        driver = self.driver
        try:
            for v in self.validators:
                v.validate_update(context, old_loadbalancer, loadbalancer)

            agent, device = self._schedule_agent_and_device(context,
                                                            loadbalancer)
            service = self._create_service(context, loadbalancer, agent)
            service["device"] = device
            agent_host = agent['host']

            driver.agent_rpc.update_loadbalancer(
                context,
                old_loadbalancer.to_api_dict(),
                loadbalancer.to_api_dict(),
                service,
                agent_host
            )
        except Exception as e:
            LOG.error("Exception: loadbalancer update: %s" % e.message)
            self._handle_entity_error(context, loadbalancer.id)
            raise e

    @log_helpers.log_method_call
    def delete(self, context, loadbalancer):
        """Delete a loadbalancer."""

        self._log_entity(loadbalancer)

        driver = self.driver
        try:
            agent, device = self._schedule_agent_and_device(context,
                                                            loadbalancer)
            if agent and device:
                # NOTE(qzhao): Call agent to delete LB.
                service = self._create_service(context, loadbalancer, agent)
                service["device"] = device
                agent_host = agent['host']

                driver.agent_rpc.delete_loadbalancer(
                    context, loadbalancer.to_api_dict(), service, agent_host)
            else:
                # NOTE(qzhao): Silently delete LB, if no binding information
                driver.plugin.db.delete_loadbalancer(context, loadbalancer.id)
        except Exception as e:
            LOG.error("Exception: loadbalancer delete: %s" % e)
            self._handle_entity_error(context, loadbalancer.id)
            raise e

    @log_helpers.log_method_call
    def refresh(self, context, loadbalancer):
        """Refresh a loadbalancer."""
        pass

    @log_helpers.log_method_call
    def stats(self, context, loadbalancer):
        driver = self.driver
        try:
            agent = driver.agent_scheduler.schedule(
                driver.plugin,
                context,
                loadbalancer,
                driver.env
            )
            service = driver.service_builder.build(context,
                                                   loadbalancer,
                                                   agent)
            driver.agent_rpc.update_loadbalancer_stats(
                context,
                loadbalancer.to_api_dict(),
                service,
                agent['host']
            )
        except Exception as e:
            LOG.error("Exception: update_loadbalancer_stats: %s" % e.message)
            self._handle_entity_error(context, loadbalancer.id)
            raise e


class ListenerManager(EntityManager):
    """ListenerManager class handles Neutron LBaaS listener CRUD."""

    def __init__(self, driver):
        super(ListenerManager, self).__init__(driver)
        self.model = models.Listener

    @log_helpers.log_method_call
    def create(self, context, listener):
        """Create a listener."""

        self._log_entity(listener)

        lb = listener.loadbalancer
        api_dict = listener.to_dict(loadbalancer=False, default_pool=False)

        def append_listeners(context, loadbalancer, service):
            self._append_listeners(context, service, listener)

        def append_pools_monitors(context, loadbalancer, service):
            if listener.default_pool:
                for pool in loadbalancer.pools:
                    if pool.id == listener.default_pool.id:
                        self._append_pools_monitors(context, service, pool)
                        break

        try:
            if cfg.CONF.f5_driver_perf_mode in (2, 3):
                # Listener may have default pool who are already created.
                # Utilize default behavior to append members
                # Listener does not have l7policies.
                self._call_rpc(
                    context, lb, listener, api_dict, 'create_listener',
                    append_listeners=append_listeners,
                    append_pools_monitors=append_pools_monitors,
                    append_l7policies_rules=lambda *args: None
                )
            else:
                self._call_rpc(context, lb, listener, api_dict,
                               'create_listener')
        except Exception as e:
            LOG.error("Exception: listener create: %s" % e.message)
            self._handle_entity_error(context, listener.id,
                                      loadbalancer_id=lb.id)
            raise e

    @log_helpers.log_method_call
    def update(self, context, old_listener, listener):
        """Update a listener."""

        self._log_entity(old_listener)
        self._log_entity(listener)

        driver = self.driver
        lb = listener.loadbalancer
        try:
            agent_host, service = self._setup_crud(context, lb, listener)
            driver.agent_rpc.update_listener(
                context,
                old_listener.to_dict(loadbalancer=False,
                                     default_pool=False),
                listener.to_dict(loadbalancer=False, default_pool=False),
                service,
                agent_host
            )
        except Exception as e:
            LOG.error("Exception: listener update: %s" % e.message)
            self._handle_entity_error(context, listener.id,
                                      loadbalancer_id=lb.id)
            raise e

    @log_helpers.log_method_call
    def delete(self, context, listener):
        """Delete a listener."""

        self._log_entity(listener)

        lb = listener.loadbalancer
        api_dict = listener.to_dict(loadbalancer=False, default_pool=False)

        def append_listeners(context, lb, service):
            self._append_listeners(context, service, listener)

        try:
            if cfg.CONF.f5_driver_perf_mode in (2, 3):
                # L7policy should already be deleted.
                # Needn't modify pool.
                self._call_rpc(
                    context, lb, listener, api_dict, 'delete_listener',
                    append_listeners=append_listeners,
                    append_pools_monitors=lambda *args: None,
                    append_members=lambda *args: None,
                    append_l7policies_rules=lambda *args: None
                )
            else:
                self._call_rpc(context, lb, listener, api_dict,
                               'delete_listener')
        except Exception as e:
            LOG.error("Exception: listener delete: %s" % e.message)
            self._handle_entity_error(context, listener.id,
                                      loadbalancer_id=lb.id)
            raise e


class PoolManager(EntityManager):
    """PoolManager class handles Neutron LBaaS pool CRUD."""

    def __init__(self, driver):
        super(PoolManager, self).__init__(driver)
        self.model = models.PoolV2

    def _get_pool_dict(self, pool):
        pool_dict = pool.to_dict(
            healthmonitor=False,
            listener=False,
            listeners=False,
            loadbalancer=False,
            l7_policies=False,
            members=False,
            session_persistence=False)

        if pool.session_persistence:
            pool_dict['session_persistence'] = (
                pool.session_persistence.to_api_dict())

        pool_dict['provisioning_status'] = pool.provisioning_status
        pool_dict['operating_status'] = pool.operating_status
        return pool_dict

    @log_helpers.log_method_call
    def create(self, context, pool):
        """Create a pool."""

        self._log_entity(pool)

        lb = pool.loadbalancer
        api_dict = self._get_pool_dict(pool)

        def append_listeners(context, loadbalancer, service):
            for listener in loadbalancer.listeners:
                if listener.default_pool:
                    if listener.default_pool.id == pool.id:
                        LOG.debug("listener %s has default pool %s",
                                  listener.id, pool.id)
                        self._append_listeners(context, service, listener)

        def append_pools_monitors(context, loadbalancer, service):
            self._append_pools_monitors(context, service, pool)

        try:
            if cfg.CONF.f5_driver_perf_mode in (2, 3):
                # Pool and l7plicies ???
                # Pool may be associated with listener, maybe not.
                # Pool has no members
                # Listener may have l7policies. Utilize default behavior.
                self._call_rpc(
                    context, lb, pool, api_dict, 'create_pool',
                    append_listeners=append_listeners,
                    append_pools_monitors=append_pools_monitors,
                    append_members=lambda *args: None
                )
            else:
                self._call_rpc(context, lb, pool, api_dict, 'create_pool')
        except Exception as e:
            LOG.error("Exception: pool create: %s" % e.message)
            self._handle_entity_error(context, pool.id,
                                      loadbalancer_id=lb.id)
            raise e

    @log_helpers.log_method_call
    def update(self, context, old_pool, pool):
        """Update a pool."""

        self._log_entity(old_pool)
        self._log_entity(pool)

        driver = self.driver
        lb = pool.loadbalancer
        try:
            agent_host, service = self._setup_crud(context, lb, pool)
            driver.agent_rpc.update_pool(
                context,
                self._get_pool_dict(old_pool),
                self._get_pool_dict(pool),
                service,
                agent_host
            )
        except Exception as e:
            LOG.error("Exception: pool update: %s" % e.message)
            self._handle_entity_error(context, pool.id,
                                      loadbalancer_id=lb.id)
            raise e

    @log_helpers.log_method_call
    def delete(self, context, pool):
        """Delete a pool."""

        self._log_entity(pool)

        lb = pool.loadbalancer
        api_dict = self._get_pool_dict(pool)

        def append_listeners(context, loadbalancer, service):
            for listener in loadbalancer.listeners:
                if listener.default_pool:
                    if listener.default_pool.id == pool.id:
                        LOG.debug("listener %s has default pool %s",
                                  listener.id, pool.id)
                        self._append_listeners(context, service, listener)

        def append_pools_monitors(context, loadbalancer, service):
            self._append_pools_monitors(context, service, pool)

        try:
            if cfg.CONF.f5_driver_perf_mode in (2, 3):
                # Pool may be associated with a listener
                # Utilize default behavior to load member, l7policy and rule
                self._call_rpc(
                    context, lb, pool, api_dict, 'delete_pool',
                    append_listeners=append_listeners,
                    append_pools_monitors=append_pools_monitors
                )
            else:
                self._call_rpc(context, lb, pool, api_dict, 'delete_pool')
        except Exception as e:
            LOG.error("Exception: pool delete: %s" % e.message)
            self._handle_entity_error(context, pool.id,
                                      loadbalancer_id=lb.id)
            raise e


class MemberManager(EntityManager):
    """MemberManager class handles Neutron LBaaS pool member CRUD."""

    def __init__(self, driver):
        super(MemberManager, self).__init__(driver)
        self.model = models.MemberV2

    @log_helpers.log_method_call
    def create(self, context, member):
        """Create a member."""

        self._log_entity(member)

        driver = self.driver
        lb = member.pool.loadbalancer

        # Refuse to create member along with another tenant's subnet
        subnet = driver.plugin.db._core_plugin.get_subnet(
            context, member.subnet_id
        )
        if member.tenant_id != subnet["tenant_id"]:
            network = driver.plugin.db._core_plugin.get_network(
                context, subnet["network_id"]
            )
            if not network["shared"]:
                raise Exception(
                    "Member and subnet are not belong to the same tenant"
                )

        the_port_id = None
        driver = self.driver

        filters = {
            'device_owner': ['network:f5lbaasv2'],
            'fixed_ips': {'subnet_id': [member.subnet_id]}
        }
        LOG.debug('fetching certain ports details:')
        all_ports = driver.plugin.db._core_plugin.get_ports_count(
            context, filters
        )
        LOG.debug("all_ports length:")
        LOG.debug(all_ports)

        if all_ports < 1:
            # agent_host, service = self._setup_crud(context, lb, member)
            agent_host = 'temp'
            LOG.info('running here')
            if member.attached_to_loadbalancer() and lb:
                LOG.info('scheduing here instead')
                this_agent = self.driver.agent_scheduler.schedule(
                    self.driver.plugin,
                    context,
                    lb,
                    self.driver.env
                )
                LOG.info(this_agent)
                agent_host = this_agent.get('host')
                LOG.info(agent_host)

            p = driver.plugin.db._core_plugin.create_port(context, {
                'port': {
                    'tenant_id': subnet['tenant_id'],
                    'network_id': subnet['network_id'],
                    'mac_address': n_const.ATTR_NOT_SPECIFIED,
                    'fixed_ips': n_const.ATTR_NOT_SPECIFIED,
                    'device_id': member.id,
                    'device_owner': 'network:f5lbaasv2',
                    'admin_state_up': member.admin_state_up,
                    'name': 'pool_port_' + member.id,
                    portbindings.HOST_ID: agent_host}})
            LOG.debug('the port created here is: %s' % p)
            the_port_id = p['id']

        api_dict = member.to_dict(pool=False)

        def append_pools_monitors(context, loadbalancer, service):
            self._append_pools_monitors(context, service, member.pool)

        LOG.info('the_port_id is:')
        LOG.info(the_port_id)

        try:
            if cfg.CONF.f5_driver_perf_mode in (2, 3):
                # Utilize default behavior to append all members
                self._call_rpc(
                    context, lb, member, api_dict, 'create_member',
                    append_listeners=lambda *args: None,
                    append_pools_monitors=append_pools_monitors,
                    append_l7policies_rules=lambda *args: None,
                    the_port_id=the_port_id
                )
            else:
                self._call_rpc(
                    context, lb, member, api_dict, 'create_member',
                    the_port_id=the_port_id
                )
        except Exception as e:
            LOG.error("Exception: member create: %s" % e.message)
            self._handle_entity_error(context, member.id,
                                      loadbalancer_id=lb.id)
            raise e

    @log_helpers.log_method_call
    def update(self, context, old_member, member):
        """Update a member."""

        self._log_entity(old_member)
        self._log_entity(member)

        driver = self.driver
        lb = member.pool.loadbalancer
        try:
            agent_host, service = self._setup_crud(context, lb, member)
            driver.agent_rpc.update_member(
                context,
                old_member.to_dict(pool=False),
                member.to_dict(pool=False),
                service,
                agent_host
            )
        except Exception as e:
            LOG.error("Exception: member update: %s" % e.message)
            self._handle_entity_error(context, member.id,
                                      loadbalancer_id=lb.id)
            raise e

    @log_helpers.log_method_call
    def delete(self, context, member):
        """Delete a member."""

        self._log_entity(member)

        lb = member.pool.loadbalancer
        driver = self.driver

        def append_pools_monitors(context, loadbalancer, service):
            self._append_pools_monitors(context, service, member.pool)

        try:
            if cfg.CONF.f5_driver_perf_mode in (2, 3):
                # Utilize default behavior to append all members
                agent_host, service = self._setup_crud(
                    context, lb, member,
                    append_listeners=lambda *args: None,
                    append_pools_monitors=append_pools_monitors,
                    append_l7policies_rules=lambda *args: None
                )
            else:
                agent_host, service = self._setup_crud(context, lb, member)

            driver.agent_rpc.delete_member(
                context, member.to_dict(pool=False), service, agent_host)
        except Exception as e:
            LOG.error("Exception: member delete: %s" % e.message)
            self._handle_entity_error(context, member.id,
                                      loadbalancer_id=lb.id)
            raise e


class HealthMonitorManager(EntityManager):
    """HealthMonitorManager class handles Neutron LBaaS monitor CRUD."""

    def __init__(self, driver):
        super(HealthMonitorManager, self).__init__(driver)
        self.model = models.HealthMonitorV2

    @log_helpers.log_method_call
    def create(self, context, health_monitor):
        """Create a health monitor."""

        self._log_entity(health_monitor)

        lb = health_monitor.pool.loadbalancer
        api_dict = health_monitor.to_dict(pool=False)

        def append_pools_monitors(context, loadbalancer, service):
            self._append_pools_monitors(context, service, health_monitor.pool)

        try:
            if cfg.CONF.f5_driver_perf_mode in (2, 3):
                # Utilize default behavior to append all members
                self._call_rpc(
                    context, lb, health_monitor, api_dict,
                    'create_health_monitor',
                    append_listeners=lambda *args: None,
                    append_pools_monitors=append_pools_monitors,
                    append_l7policies_rules=lambda *args: None
                )
            else:
                self._call_rpc(context, lb, health_monitor, api_dict,
                               'create_health_monitor')
        except Exception as e:
            LOG.error("Exception: health monitor create: %s" % e.message)
            self._handle_entity_error(context, health_monitor.id,
                                      loadbalancer_id=lb.id)
            raise e

    @log_helpers.log_method_call
    def update(self, context, old_health_monitor, health_monitor):
        """Update a health monitor."""

        self._log_entity(old_health_monitor)
        self._log_entity(health_monitor)

        driver = self.driver
        lb = health_monitor.pool.loadbalancer
        try:
            agent_host, service = self._setup_crud(context, lb, health_monitor)
            driver.agent_rpc.update_health_monitor(
                context,
                old_health_monitor.to_dict(pool=False),
                health_monitor.to_dict(pool=False),
                service,
                agent_host
            )
        except Exception as e:
            LOG.error("Exception: health monitor update: %s" % e.message)
            self._handle_entity_error(context, health_monitor.id,
                                      loadbalancer_id=lb.id)
            raise e

    @log_helpers.log_method_call
    def delete(self, context, health_monitor):
        """Delete a health monitor."""

        self._log_entity(health_monitor)

        lb = health_monitor.pool.loadbalancer
        api_dict = health_monitor.to_dict(pool=False)

        def append_pools_monitors(context, loadbalancer, service):
            self._append_pools_monitors(context, service, health_monitor.pool)

        try:
            if cfg.CONF.f5_driver_perf_mode in (2, 3):
                # Utilize default behavior to append all members
                self._call_rpc(
                    context, lb, health_monitor, api_dict,
                    'delete_health_monitor',
                    append_listeners=lambda *args: None,
                    append_pools_monitors=append_pools_monitors,
                    append_l7policies_rules=lambda *args: None
                )
            else:
                self._call_rpc(context, lb, health_monitor, api_dict,
                               'delete_health_monitor')
        except Exception as e:
            LOG.error("Exception: health monitor delete: %s" % e.message)
            self._handle_entity_error(context, health_monitor.id,
                                      loadbalancer_id=lb.id)
            raise e


class L7PolicyManager(EntityManager):
    """L7PolicyManager class handles Neutron LBaaS L7 Policy CRUD."""

    def __init__(self, driver):
        super(L7PolicyManager, self).__init__(driver)
        self.model = models.L7Policy

    @log_helpers.log_method_call
    def create(self, context, policy):
        """Create an L7 policy."""

        self._log_entity(policy)

        lb = policy.listener.loadbalancer
        api_dict = policy.to_dict(listener=False, rules=False)

        def append_listeners(context, loadbalancer, service):
            self._append_listeners(context, service, policy.listener)

        def append_pools_monitors(context, loadbalancer, service):
            self._append_pools_monitors(
                context, service, policy.listener.default_pool)

        try:
            if cfg.CONF.f5_driver_perf_mode in (2, 3):
                # Utilize default behavior to load policies and rules
                # Listener may have default pool
                # Utilize default behavior to load members
                self._call_rpc(
                    context, lb, policy, api_dict, 'create_l7policy',
                    append_listeners=append_listeners,
                    append_pools_monitors=append_pools_monitors
                )
            else:
                self._call_rpc(context, lb, policy, api_dict,
                               'create_l7policy')
        except Exception as e:
            LOG.error("Exception: l7policy create: %s" % e.message)
            self._handle_entity_error(context, policy.id,
                                      loadbalancer_id=lb.id)
            raise e

    @log_helpers.log_method_call
    def update(self, context, old_policy, policy):
        """Update a policy."""

        self._log_entity(old_policy)
        self._log_entity(policy)

        driver = self.driver
        lb = policy.listener.loadbalancer
        try:
            agent_host, service = self._setup_crud(context, lb, policy)
            driver.agent_rpc.update_l7policy(
                context,
                old_policy.to_dict(listener=False),
                policy.to_dict(listener=False),
                service,
                agent_host
            )
        except Exception as e:
            LOG.error("Exception: l7policy update: %s" % e.message)
            self._handle_entity_error(context, policy.id,
                                      loadbalancer_id=lb.id)
            raise e

    @log_helpers.log_method_call
    def delete(self, context, policy):
        """Delete a policy."""

        self._log_entity(policy)

        lb = policy.listener.loadbalancer
        api_dict = policy.to_dict(listener=False, rules=False)

        def append_listeners(context, loadbalancer, service):
            self._append_listeners(context, service, policy.listener)

        def append_pools_monitors(context, loadbalancer, service):
            self._append_pools_monitors(
                context, service, policy.listener.default_pool)

        try:
            if cfg.CONF.f5_driver_perf_mode in (2, 3):
                # Utilize default behavior to load policies and rules
                # Listener may have default pool
                # Utilize default behavior to load members
                self._call_rpc(
                    context, lb, policy, api_dict, 'delete_l7policy',
                    append_listeners=append_listeners,
                    append_pools_monitors=append_pools_monitors
                )
            else:
                self._call_rpc(context, lb, policy, api_dict,
                               'delete_l7policy')
        except Exception as e:
            LOG.error("Exception: l7policy delete: %s" % e.message)
            self._handle_entity_error(context, policy.id,
                                      loadbalancer_id=lb.id)
            raise e


class L7RuleManager(EntityManager):
    """L7RuleManager class handles Neutron LBaaS L7 Rule CRUD."""

    def __init__(self, driver):
        super(L7RuleManager, self).__init__(driver)
        self.model = models.L7Rule

    @log_helpers.log_method_call
    def create(self, context, rule):
        """Create an L7 rule."""

        self._log_entity(rule)

        lb = rule.policy.listener.loadbalancer
        api_dict = rule.to_dict(policy=False)

        def append_listeners(context, loadbalancer, service):
            self._append_listeners(context, service, rule.policy.listener)

        def append_pools_monitors(context, loadbalancer, service):
            self._append_pools_monitors(
                context, service, rule.policy.listener.default_pool)

        try:
            if cfg.CONF.f5_driver_perf_mode in (2, 3):
                # Utilize default behavior to load policies and rules
                # Listener may have default pool
                # Utilize default behavior to load members
                self._call_rpc(
                    context, lb, rule, api_dict, 'create_l7rule',
                    append_listeners=append_listeners,
                    append_pools_monitors=append_pools_monitors
                )
            else:
                self._call_rpc(context, lb, rule, api_dict, 'create_l7rule')
        except Exception as e:
            LOG.error("Exception: l7rule create: %s" % e.message)
            self._handle_entity_error(context, rule.id,
                                      loadbalancer_id=lb.id)
            raise e

    @log_helpers.log_method_call
    def update(self, context, old_rule, rule):
        """Update a rule."""

        self._log_entity(old_rule)
        self._log_entity(rule)

        driver = self.driver
        lb = rule.policy.listener.loadbalancer
        try:
            agent_host, service = self._setup_crud(context, lb, rule)
            driver.agent_rpc.update_l7rule(
                context,
                old_rule.to_dict(policy=False),
                rule.to_dict(policy=False),
                service,
                agent_host
            )
        except Exception as e:
            LOG.error("Exception: l7rule update: %s" % e.message)
            self._handle_entity_error(context, rule.id,
                                      loadbalancer_id=lb.id)
            raise e

    @log_helpers.log_method_call
    def delete(self, context, rule):
        """Delete a rule."""

        self._log_entity(rule)

        lb = rule.policy.listener.loadbalancer
        api_dict = rule.to_dict(policy=False)

        def append_listeners(context, loadbalancer, service):
            self._append_listeners(context, service, rule.policy.listener)

        def append_pools_monitors(context, loadbalancer, service):
            self._append_pools_monitors(
                context, service, rule.policy.listener.default_pool)

        try:
            if cfg.CONF.f5_driver_perf_mode in (2, 3):
                # Utilize default behavior to load policies and rules
                # Listener may have default pool
                # Utilize default behavior to load members
                self._call_rpc(
                    context, lb, rule, api_dict, 'delete_l7rule',
                    append_listeners=append_listeners,
                    append_pools_monitors=append_pools_monitors
                )
            else:
                self._call_rpc(context, lb, rule, api_dict, 'delete_l7rule')
        except Exception as e:
            LOG.error("Exception: l7rule delete: %s" % e.message)
            self._handle_entity_error(context, rule.id,
                                      loadbalancer_id=lb.id)
            raise e


class ACLGroupManager(EntityManager):

    def __init__(self, driver):
        super(ACLGroupManager, self).__init__(driver)

        # in case of break pipeline
        try:
            self.model = models.ACLGroup
        except AttributeError as ex:
            LOG.warn(ex.message)

    def _setup_crud(self, context, loadbalancer, loadbalancer_dict):
        '''Setup CRUD operations for managers to make calls to agent.

        :param context: auth context for performing CRUD operation
        :returns: tuple -- (agent object, service dict)
        :raises: F5NoAttachedLoadbalancerException
        '''

        service = dict()

        agent, device = self._schedule_agent_and_device(
            context, loadbalancer)

        service['loadbalancer'] = loadbalancer_dict
        service["device"] = device
        return agent['host'], service

    @log_helpers.log_method_call
    def add_acl_bind(
        self, context, acl_bind, loadbalancer_dict,
        listener, acl_group
    ):
        provider = self.driver.env.lower()
        if loadbalancer_dict["provider"] != provider:
            LOG.debug(
                "the provider of loadbalancer %s is not the "
                "same with %s." % (
                    loadbalancer_dict, provider)
            )
            return

        loadbalancer = data_models.LoadBalancer(
            **loadbalancer_dict)

        agent_host = None
        try:
            agent_host, service = self._setup_crud(
                context, loadbalancer,
                loadbalancer_dict
            )

            # if not put this. F5 agent will call
            # _search_element, and throw error
            service['acl_group'] = acl_group

            # 1. Create a ACL Data group
            # 2. Add a ACL binding of listener.
            self.driver.agent_rpc.add_acl_bind(
                context,
                listener,
                acl_group,
                acl_bind,
                service,
                agent_host
            )
        except Exception as ex:
            msg = "Fail to add ACL bind of listener. \n" \
                "Listener: %s \n" \
                "ACL binding: %s \n" \
                "ACL group: %s \n" \
                "Agent host %s \n" % (
                    listener, acl_bind, acl_group,
                    agent_host
                )
            LOG.exception(msg)
            raise f5_exc.ACLBindError(str(ex))

    @log_helpers.log_method_call
    def remove_acl_bind(
        self, context, acl_bind, loadbalancer_dict,
        listener, acl_group
    ):

        provider = self.driver.env.lower()
        if loadbalancer_dict["provider"] != provider:
            LOG.debug(
                "the provider of loadbalancer %s is not the "
                "same with %s." % (
                    loadbalancer_dict, provider)
            )
            return

        loadbalancer = data_models.LoadBalancer(
            **loadbalancer_dict)

        agent_host = None
        try:
            agent_host, service = self._setup_crud(
                context, loadbalancer,
                loadbalancer_dict
            )

            # if not put this. F5 agent will call
            # _search_element, and throw error
            service['acl_group'] = acl_group

            # 1. Remove a ACL binding of listener.
            # 2. Try to delete the shared ACL Data group.
            self.driver.agent_rpc.remove_acl_bind(
                context,
                listener,
                acl_group,
                acl_bind,
                service,
                agent_host
            )
        except Exception as ex:
            msg = "Fail to remove ACL bind of listener. \n" \
                "Listener: %s \n" \
                "ACL binding: %s \n" \
                "ACL group: %s \n" \
                "Agent host %s" % (
                    listener, acl_bind, acl_group,
                    agent_host
                )
            LOG.exception(msg)
            raise f5_exc.ACLBindError(str(ex))

    @log_helpers.log_method_call
    def update_acl_group(self, context, acl_group, loadbalancers):

        service_devices = dict()

        provider = self.driver.env.lower()
        loadbalancers = [
            lb for lb in loadbalancers if lb[
                "provider"
            ] == provider
        ]

        if not loadbalancers:
            LOG.debug(
                "the provider of loadbalancers %s is not the "
                "same with %s." % (loadbalancers, provider)
            )
            return

        agent_host = None
        try:
            for loadbalancer_dict in loadbalancers:
                loadbalancer = data_models.LoadBalancer(
                    **loadbalancer_dict)

                agent_host, service = self._setup_crud(
                    context, loadbalancer,
                    loadbalancer_dict
                )

                service_devices[
                    service["device"]["id"]
                ] = {"agent_host": agent_host, "service": service}

            for device in service_devices.values():
                agent_host = device["agent_host"]
                service = device["service"]

                # if not put this. F5 agent will call
                # _search_element, and throw error
                service['acl_group'] = acl_group

                self.driver.agent_rpc.update_acl_group(
                    context, acl_group, service, agent_host
                )
        except Exception as ex:
            msg = "Fail to update ACL group.\n" \
                "ACL group: %s \n" \
                "Agent host %s" % (
                    acl_group, agent_host
                )
            LOG.exception(msg)
            raise f5_exc.ACLGroupUpdateError(str(ex))
