#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import time

from twitter.common import log
from twitter.common.quantity import Amount, Time

from apache.aurora.client.api import AuroraClientAPI
from apache.aurora.client.base import check_and_log_response, DEFAULT_GROUPING, group_hosts

from gen.apache.aurora.api.ttypes import Hosts, MaintenanceMode


class HostMaintenance(object):
  """Submit requests to the scheduler to put hosts into and out of maintenance
  mode so they can be operated upon without causing LOST tasks.

  Aurora provides a two-tiered concept of Maintenance. The first step is to initiate maintenance,
  which will ask the Aurora scheduler to de-prioritize scheduling on a large set of hosts (the ones
  that will be operated upon during this maintenance window).  Once all hosts have been tagged in
  this manner, the operator can begin draining individual machines, which will have all user-tasks
  killed and rescheduled.  When the tasks get placed onto a new machine, the scheduler will first
  look for hosts that do not have the maintenance tag, which will help decrease churn and prevent a
  task from being constantly killed as its hosts go down from underneath it.
  """

  START_MAINTENANCE_DELAY = Amount(30, Time.SECONDS)

  @classmethod
  def iter_batches(cls, hostnames, grouping_function=DEFAULT_GROUPING):
    groups = group_hosts(hostnames, grouping_function)
    groups = sorted(groups.items(), key=lambda v: v[0])
    for group in groups:
      yield Hosts(group[1])

  def __init__(self, cluster, verbosity):
    self._client = AuroraClientAPI(cluster, verbosity == 'verbose')

  def _drain_hosts(self, drainable_hosts, clock=time):
    """"Drains tasks from the specified hosts.

    This will move active tasks on these hosts to the DRAINING state, causing them to be
    rescheduled elsewhere.

    :param drainable_hosts: Hosts that are in maintenance mode and ready to be drained
    :type drainable_hosts: gen.apache.aurora.ttypes.Hosts
    :param clock: time module for testing
    :type clock: time
    """
    check_and_log_response(self._client.drain_hosts(drainable_hosts))
    not_ready_hostnames = [hostname for hostname in drainable_hosts.hostNames]
    while not_ready_hostnames:
      log.info("Sleeping for %s." % self.START_MAINTENANCE_DELAY)
      clock.sleep(self.START_MAINTENANCE_DELAY.as_(Time.SECONDS))
      resp = self._client.maintenance_status(Hosts(set(not_ready_hostnames)))
      if not resp.result.maintenanceStatusResult.statuses:
        not_ready_hostnames = None
      for host_status in resp.result.maintenanceStatusResult.statuses:
        if host_status.mode != MaintenanceMode.DRAINED:
          log.warning('%s is currently in status %s' %
              (host_status.host, MaintenanceMode._VALUES_TO_NAMES[host_status.mode]))
        else:
          not_ready_hostnames.remove(host_status.host)

  def _complete_maintenance(self, drained_hosts):
    """End the maintenance status for a given set of hosts.

    :param drained_hosts: Hosts that are drained and finished being operated upon
    :type drained_hosts: gen.apache.aurora.ttypes.Hosts
    """
    check_and_log_response(self._client.end_maintenance(drained_hosts))
    resp = self._client.maintenance_status(drained_hosts)
    for host_status in resp.result.maintenanceStatusResult.statuses:
      if host_status.mode != MaintenanceMode.NONE:
        log.warning('%s is DRAINING or in DRAINED' % host_status.host)

  def _operate_on_hosts(self, drained_hosts, callback):
    """Perform a given operation on a list of hosts that are ready for maintenance.

    :param drained_hosts: Hosts that have been drained (via _drain_hosts)
    :type drained_hosts: gen.apache.aurora.ttypes.Hosts
    :param callback: Function to call one hostname at a time
    :type callback: function
    """
    for hostname in drained_hosts.hostNames:
      callback(hostname)

  def end_maintenance(self, hostnames):
    """Pull a list of hostnames out of maintenance mode.

    :param hostnames: List of hosts to operate upon
    :type hostnames: list of strings
    """
    self._complete_maintenance(Hosts(set(hostnames)))

  def start_maintenance(self, hostnames):
    """Put a list of hostnames into maintenance mode, to de-prioritize scheduling.

    This is part of two-phase draining- tasks will still be running on these hosts until
    drain_hosts is called upon them.

    :param hostnames: List of hosts to set for initial maintenance
    :type hostnames: list of strings
    """
    check_and_log_response(self._client.start_maintenance(Hosts(set(hostnames))))

  def perform_maintenance(self, hostnames, grouping_function=DEFAULT_GROUPING,
                          callback=None):
    """Wrap a callback in between sending hosts into maintenance mode and back.

    Walk through the process of putting hosts into maintenance, draining them of tasks,
    performing an action on them once drained, then removing them from maintenance mode
    so tasks can schedule.

    :param hostnames: A list of hosts to operate upon
    :type hostnames: list of strings
    :param groups_per_batch: Number of groups (by default, hosts) to operate on at once
    :type groups_per_batch: int
    :param grouping_function: How to split up the hostname into groups
    :type grouping_function: function
    :param callback: Function to call once hosts are drained
    :type callback: function
    """
    self.start_maintenance(hostnames)

    for hosts in self.iter_batches(hostnames, grouping_function):
      self._drain_hosts(hosts)
      if callback:
        self._operate_on_hosts(hosts, callback)
      self._complete_maintenance(hosts)

  def check_status(self, hostnames):
    """Query the scheduler to determine the maintenance status for a list of hostnames

    :param hostnames: Hosts to query for
    :type hostnames: list of strings
    :rtype: list of 2-tuples, hostname and MaintenanceMode
    """
    resp = self._client.maintenance_status(Hosts(set(hostnames)))
    check_and_log_response(resp)
    statuses = []
    for host_status in resp.result.maintenanceStatusResult.statuses:
      statuses.append((host_status.host, MaintenanceMode._VALUES_TO_NAMES[host_status.mode]))
    return statuses
