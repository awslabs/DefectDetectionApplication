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
        # This operation will not send or receive any data until activate() is called.
        # Call activate() when you’re ready for callbacks and events to fire.
        self.config_request = self.ipc_client.new_get_configuration()
        self.config_cache = dict()
        self.lock = Lock()

    def get_component_config(self, name: str):
        # guarantee thread-safe when this is called by multiple threads
        with self.lock:
            request = model.GetConfigurationRequest(component_name=name)
            self.config_request.activate(request)
            full_response = self.config_request.get_response()
            
            try:
                value = full_response.result(TIME_OUT).value
            except model.UnauthorizedError as ue:
                logger.error('Unauthorized error while fetching config for component: ' + name)
                value = {}
            except Exception as e:
                logger.error('Exception occurred: '+ str(e))
                value = {}
            
            self.config_request.close()
        return value

     
    def get_local_server_config(self):
        if DDA_LOCAL_SERVER_COMPONENT in self.config_cache.keys():
            return self.config_cache.get(DDA_LOCAL_SERVER_COMPONENT)

        local_server_config = self.get_component_config(name=DDA_LOCAL_SERVER_COMPONENT)
        if local_server_config:
            self.config_cache[DDA_LOCAL_SERVER_COMPONENT] = local_server_config
        
        return local_server_config


    def get_station(self):
        station_name = self.get_local_server_config().get(STATION_NAME_KEY)
        # setting a generic name if Station config doesn't have stationName key, although we do this in the UI code as well
        if (not(station_name and not station_name.isspace())):
            station_name = GENERIC_STATION_NAME

        version = self.get_local_server_config().get(SOFTWARE_VERSION_KEY)
        if version is None or version.isspace():
            version = UNKNOWN_VERSION
            
        webux_url = self.get_local_server_config().get(WEBUX_KEY)
        tenant_id = self.get_local_server_config().get(TENANT_ID_KEY)
        device_id = self.get_local_server_config().get(DEVICE_ID_KEY)       
        
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