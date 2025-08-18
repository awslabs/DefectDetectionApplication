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

import typing
from abc import abstractmethod

from panorama import panorama_projections
from panorama import unknown
from panorama import properties
from panorama import credentials
from panorama import gst
from panorama import apidefs
from panorama import trace
from panorama import buffer
from panorama import application
from panorama import messagebroker

class AppPlugin(unknown.UnknownImpl, panorama_projections.IAppPlugin):
    def __init__(self):
        panorama_projections.IAppPlugin.__init__(self)
        unknown.UnknownImpl.__init__(self)
        self.uuid = "906B0ECC-1C59-47E5-AAF4-1F1C8FBD8B3E"

    def Initialize(self, app: panorama_projections.IApp, message_broker: panorama_projections.IMessageBroker):
        py_app = apidefs.assign(app, lambda x: application.App(x))
        py_message_broker = apidefs.assign(message_broker, lambda x: messagebroker.MessageBroker(x))

        try:
            self.initialize(py_app, py_app, py_message_broker)
        except Exception as e:
            trace.error(f"Exception raised when calling AppPlugin::initialize. {e}")
            return apidefs.E_FAIL

        return apidefs.S_OK
    
    def OnPipelineError(self, error: panorama_projections.IPipelineError):
        pipeline_error = apidefs.assign(error, lambda x: gst.PipelineError(x))
        try:
            self.on_pipeline_error(pipeline_error)
            return apidefs.S_OK
        except Exception as e:
            trace.error(f"Exception raised when calling AppPlugin::on_pipeline_error. {e}")
            return apidefs.E_FAIL

    def OnPropertiesChanged(self, changed_properties: panorama_projections.IPropertyCollection):
        py_changed_properties = apidefs.assign(changed_properties, lambda x: properties.PropertyCollection(x))
        try:
            self.on_properties_changed(py_changed_properties)
            return apidefs.S_OK
        except Exception as e:
            trace.error(f"Exception raised when calling AppPlugin::on_properties_changed. {e}")
            return apidefs.E_FAIL

    def Shutdown(self):
        try:
            self.shutdown()
            return apidefs.S_OK
        except Exception as e:
            trace.error(f"Exception raised when calling AppPlugin::shutdown. {e}")
            return apidefs.E_FAIL

    def Id(self):
        try:
            id = self.id()
            if id is None:
                raise Exception("Id returned from plugin cannot be None")
            return id
        except Exception as e:
            trace.error(f"Exception raised when calling AppPlugin::id. {e}")
            return ""

    @abstractmethod
    def initialize(self, properties: properties.PropertyDelegate, credential_provider: credentials.CredentialProvider, message_broker: messagebroker.MessageBroker):
        raise NotImplementedError("initialize has not been implemented")

    @abstractmethod
    def on_pipeline_error(self, error: gst.pipeline.PipelineError):
        raise NotImplementedError("on_pipeline_error has not been implemented")

    @abstractmethod
    def on_properties_changed(self, changed_properties: properties.PropertyCollection):
        raise NotImplementedError("on_properties_changed has not been implemented")

    @abstractmethod
    def shutdown(self):
        raise NotImplementedError("shutdown has not been implemented")

    @abstractmethod
    def id(self):
        raise NotImplementedError("id has not been implemented")