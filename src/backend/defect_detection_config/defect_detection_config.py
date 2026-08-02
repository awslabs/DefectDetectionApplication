#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
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

import sys
import logging
import awsiot.greengrasscoreipc
import awsiot.greengrasscoreipc.model as model
from model.metadata import *
from utils.constants import DDA_LOCAL_SERVER_COMPONENT
from threading import Lock
from deprecated import deprecated
import platform


TIME_OUT = 10 #seconds
STATION_NAME_KEY = "StationName"
SOFTWARE_VERSION_KEY = "SoftwareVersion"
GENERIC_STATION_NAME = "Station"
DEVICE_ID_KEY = "DeviceID"
TENANT_ID_KEY = "TenantID"
UNKNOWN_VERSION = "version unknown"
WEBUX_KEY = "WebuxUrl"

import logging
logger = logging.getLogger(__name__)

class DefectDetectionConfig:

    def __init__(self, ipc_client):
        self.ipc_client = ipc_client
        self.config_cache = dict()
        self.lock = Lock()
        # Resolved LocalServer component name, discovered once per instance.
        self._component_name = None

    def get_component_config(self, name: str):
        # guarantee thread-safe when this is called by multiple threads
        with self.lock:
            # An event-stream RPC operation (continuation) is SINGLE-USE: once
            # activate()/close() has run, the same object cannot be activated
            # again — doing so raises RuntimeError 38 (AWS_ERROR_INVALID_STATE).
            # So create a fresh GetConfiguration operation for every call rather
            # than reusing one cached in __init__ (which made the first call
            # succeed and every subsequent call fail in a loop).
            config_request = self.ipc_client.new_get_configuration()
            request = model.GetConfigurationRequest(component_name=name)
            config_request.activate(request)
            full_response = config_request.get_response()
            
            try:
                value = full_response.result(TIME_OUT).value
            except model.UnauthorizedError as ue:
                logger.error('Unauthorized error while fetching config for component: ' + name)
                value = {}
            except RuntimeError as re:
                if "AWS_ERROR_INVALID_STATE" in str(re):
                    logger.error(f'AWS_ERROR_INVALID_STATE while fetching config for component {name}. Greengrass Core may not be running properly.')
                else:
                    logger.error(f'RuntimeError occurred while fetching config for component {name}: {str(re)}')
                value = {}
            except Exception as e:
                logger.error('Exception occurred: '+ str(e))
                value = {}
            finally:
                config_request.close()
        return value
    
    def get_local_server_component_name(self):
        """Resolve the running LocalServer component name via dynamic
        discovery (ListComponents prefix match), cached per instance.

        The name cannot be derived from the CPU architecture alone: aarch64
        alone spans three distinct components (``...LocalServer.arm64`` on
        JP4, ``...arm64JP5``, ``...arm64JP6``). The previous hardcoded
        arch map returned the JP4 name on every Jetson, so JP5/JP6
        stations failed every GetConfiguration call in a retry loop.
        """
        if self._component_name:
            return self._component_name
        name = self.find_local_server_component_name()
        if name != DDA_LOCAL_SERVER_COMPONENT:
            # Cache only a real discovery; the bare prefix fallback means
            # ListComponents failed, so retry discovery on the next call.
            self._component_name = name
        return name
    
    def find_local_server_component_name(self):
        """Find the actual LocalServer component name dynamically"""
        try:
            list_request = model.ListComponentsRequest()
            list_operation = self.ipc_client.new_list_components()
            list_operation.activate(list_request)
            list_response = list_operation.get_response().result(TIME_OUT)
            
            for component in list_response.components:
                if component.component_name.startswith("aws.edgeml.dda.LocalServer"):
                    return component.component_name
                    
            logger.warning("No LocalServer component found, using default")
            return DDA_LOCAL_SERVER_COMPONENT
        except Exception as e:
            logger.error(f"Failed to list components: {e}")
            return DDA_LOCAL_SERVER_COMPONENT

     
    def get_local_server_config(self):
        component_name = self.get_local_server_component_name()

        if component_name in self.config_cache.keys():
            return self.config_cache.get(component_name)

        local_server_config = self.get_component_config(name=component_name)
        if local_server_config:
            self.config_cache[component_name] = local_server_config
        
        return local_server_config


    def get_station(self):
        config = self.get_local_server_config()
        
        # Handle case where config is empty due to Greengrass issues
        if not config:
            logger.warning("Local server config is empty, using default values")
            config = {}
        
        station_name = config.get(STATION_NAME_KEY)
        # setting a generic name if Station config doesn't have stationName key, although we do this in the UI code as well
        if (not(station_name and not station_name.isspace())):
            station_name = GENERIC_STATION_NAME

        version = config.get(SOFTWARE_VERSION_KEY)
        if version is None or version.isspace():
            version = UNKNOWN_VERSION
            
        webux_url = config.get(WEBUX_KEY)
        tenant_id = config.get(TENANT_ID_KEY)
        device_id = config.get(DEVICE_ID_KEY)       
        
        schema = StationSchema(many=False)
        return schema.dump(Station(station_name, version, device_id, tenant_id, webux_url))
    
    # The deprecation message will be logged to log file
    @deprecated(version='1.0.100.0', reason="The /station operation is deprecated. To get station name, use /system/station")
    def get_station_old(self):
        station_name = self.get_local_server_config().get(STATION_NAME_KEY)
        # setting a generic name if Station config doesn't have stationName key, although we do this in the UI code as well
        if (not(station_name and not station_name.isspace())):
            station_name = GENERIC_STATION_NAME
            
        schema = StationSchema(many=False)
        return schema.dump(Station(station_name))