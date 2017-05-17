from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import pickle
import redis

from ray.utils import (decode, binary_to_object_id, binary_to_hex,
                       hex_to_binary)

# Import flatbuffer bindings.
from ray.core.generated.TaskInfo import TaskInfo
from ray.core.generated.TaskReply import TaskReply
from ray.core.generated.ResultTableReply import ResultTableReply

# These prefixes must be kept up-to-date with the definitions in
# ray_redis_module.cc.
DB_CLIENT_PREFIX = "CL:"
OBJECT_INFO_PREFIX = "OI:"
OBJECT_LOCATION_PREFIX = "OL:"
OBJECT_SUBSCRIBE_PREFIX = "OS:"
TASK_PREFIX = "TT:"
FUNCTION_PREFIX = "RemoteFunction:"
OBJECT_CHANNEL_PREFIX = "OC:"

# This mapping from integer to task state string must be kept up-to-date with
# the scheduling_state enum in task.h.
task_state_mapping = {
    1: "WAITING",
    2: "SCHEDULED",
    4: "QUEUED",
    8: "RUNNING",
    16: "DONE",
    32: "LOST",
    64: "RECONSTRUCTING"
}


class GlobalState(object):
  """A class used to interface with the Ray control state.

  Attributes:
    redis_client: The redis client used to query the redis server.
  """
  def __init__(self):
    """Create a GlobalState object."""
    self.redis_client = None

  def _check_connected(self):
    """Check that the object has been initialized before it is used.

    Raises:
      Exception: An exception is raised if ray.init() has not been called yet.
    """
    if self.redis_client is None:
      raise Exception("The ray.global_state API cannot be used before "
                      "ray.init has been called.")

  def _initialize_global_state(self, redis_ip_address, redis_port):
    """Initialize the GlobalState object by connecting to Redis.

    Args:
      redis_ip_address: The IP address of the node that the Redis server lives
        on.
      redis_port: The port that the Redis server is listening on.
    """
    self.redis_client = redis.StrictRedis(host=redis_ip_address,
                                          port=redis_port)

  def _object_table(self, object_id_binary):
    """Fetch and parse the object table information for a single object ID.

    Args:
      object_id_binary: A string of bytes with the object ID to get information
        about.

    Returns:
      A dictionary with information about the object ID in question.
    """
    # Return information about a single object ID.
    object_locations = self.redis_client.execute_command(
        "RAY.OBJECT_TABLE_LOOKUP", object_id_binary)
    if object_locations is not None:
      manager_ids = [binary_to_hex(manager_id)
                     for manager_id in object_locations]
    else:
      manager_ids = None

    result_table_response = self.redis_client.execute_command(
        "RAY.RESULT_TABLE_LOOKUP", object_id_binary)
    result_table_message = ResultTableReply.GetRootAsResultTableReply(
        result_table_response, 0)

    result = {"ManagerIDs": manager_ids,
              "TaskID": binary_to_hex(result_table_message.TaskId()),
              "IsPut": bool(result_table_message.IsPut())}

    return result

  def object_table(self, object_id=None):
    """Fetch and parse the object table information for one or more object IDs.

    Args:
      object_id: An object ID to fetch information about. If this is None, then
        the entire object table is fetched.


    Returns:
      Information from the object table.
    """
    self._check_connected()
    if object_id is not None:
      # Return information about a single object ID.
      return self._object_table(object_id.id())
    else:
      # Return the entire object table.
      object_info_keys = self.redis_client.keys(OBJECT_INFO_PREFIX + "*")
      object_location_keys = self.redis_client.keys(
          OBJECT_LOCATION_PREFIX + "*")
      object_ids_binary = set(
          [key[len(OBJECT_INFO_PREFIX):] for key in object_info_keys] +
          [key[len(OBJECT_LOCATION_PREFIX):] for key in object_location_keys])
      results = {}
      for object_id_binary in object_ids_binary:
        results[binary_to_object_id(object_id_binary)] = self._object_table(
            object_id_binary)
      return results

  def _task_table(self, task_id_binary):
    """Fetch and parse the task table information for a single object task ID.

    Args:
      task_id_binary: A string of bytes with the task ID to get information
        about.

    Returns:
      A dictionary with information about the task ID in question.
    """
    task_table_response = self.redis_client.execute_command(
        "RAY.TASK_TABLE_GET", task_id_binary)
    if task_table_response is None:
      raise Exception("There is no entry for task ID {} in the task table."
                      .format(binary_to_hex(task_id_binary)))
    task_table_message = TaskReply.GetRootAsTaskReply(task_table_response, 0)
    task_spec = task_table_message.TaskSpec()
    task_spec_message = TaskInfo.GetRootAsTaskInfo(task_spec, 0)
    args = []
    for i in range(task_spec_message.ArgsLength()):
      arg = task_spec_message.Args(i)
      if len(arg.ObjectId()) != 0:
        args.append(binary_to_object_id(arg.ObjectId()))
      else:
        args.append(pickle.loads(arg.Data()))
    assert task_spec_message.RequiredResourcesLength() == 2
    required_resources = {"CPUs": task_spec_message.RequiredResources(0),
                          "GPUs": task_spec_message.RequiredResources(1)}
    task_spec_info = {
        "DriverID": binary_to_hex(task_spec_message.DriverId()),
        "TaskID": binary_to_hex(task_spec_message.TaskId()),
        "ParentTaskID": binary_to_hex(task_spec_message.ParentTaskId()),
        "ParentCounter": task_spec_message.ParentCounter(),
        "ActorID": binary_to_hex(task_spec_message.ActorId()),
        "ActorCounter": task_spec_message.ActorCounter(),
        "FunctionID": binary_to_hex(task_spec_message.FunctionId()),
        "Args": args,
        "ReturnObjectIDs": [binary_to_object_id(task_spec_message.Returns(i))
                            for i in range(task_spec_message.ReturnsLength())],
        "RequiredResources": required_resources}

    return {"State": task_state_mapping[task_table_message.State()],
            "LocalSchedulerID": binary_to_hex(
                task_table_message.LocalSchedulerId()),
            "TaskSpec": task_spec_info}

  def task_table(self, task_id=None):
    """Fetch and parse the task table information for one or more task IDs.

    Args:
      task_id: A hex string of the task ID to fetch information about. If this
        is None, then the task object table is fetched.


    Returns:
      Information from the task table.
    """
    self._check_connected()
    if task_id is not None:
      return self._task_table(hex_to_binary(task_id))
    else:
      task_table_keys = self.redis_client.keys(TASK_PREFIX + "*")
      results = {}
      for key in task_table_keys:
        task_id_binary = key[len(TASK_PREFIX):]
        results[binary_to_hex(task_id_binary)] = self._task_table(
            task_id_binary)
      return results

  def function_table(self, function_id=None):
    """Fetch and parse the function table.

    Returns:
      A dictionary that maps function IDs to information about the function.
    """
    self._check_connected()
    function_table_keys = self.redis_client.keys(FUNCTION_PREFIX + "*")
    results = {}
    for key in function_table_keys:
      info = self.redis_client.hgetall(key)
      function_info_parsed = {
          "DriverID": binary_to_hex(info[b"driver_id"]),
          "Module": decode(info[b"module"]),
          "Name": decode(info[b"name"])
      }
      results[binary_to_hex(info[b"function_id"])] = function_info_parsed
    return results

  def client_table(self):
    """Fetch and parse the Redis DB client table.

    Returns:
      Information about the Ray clients in the cluster.
    """
    self._check_connected()
    db_client_keys = self.redis_client.keys(DB_CLIENT_PREFIX + "*")
    node_info = dict()
    for key in db_client_keys:
      client_info = self.redis_client.hgetall(key)
      node_ip_address = decode(client_info[b"node_ip_address"])
      if node_ip_address not in node_info:
        node_info[node_ip_address] = []
      client_info_parsed = {
          "ClientType": decode(client_info[b"client_type"]),
          "Deleted": bool(int(decode(client_info[b"deleted"]))),
          "DBClientID": binary_to_hex(client_info[b"ray_client_id"])
      }
      if b"aux_address" in client_info:
        client_info_parsed["AuxAddress"] = decode(client_info[b"aux_address"])
      if b"num_cpus" in client_info:
        client_info_parsed["NumCPUs"] = float(decode(client_info[b"num_cpus"]))
      if b"num_gpus" in client_info:
        client_info_parsed["NumGPUs"] = float(decode(client_info[b"num_gpus"]))
      if b"local_scheduler_socket_name" in client_info:
        client_info_parsed["LocalSchedulerSocketName"] = decode(
            client_info[b"local_scheduler_socket_name"])
      node_info[node_ip_address].append(client_info_parsed)

    return node_info
