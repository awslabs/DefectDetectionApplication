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

from panorama import gst_application
from panorama import messagebroker
from panorama import properties
from panorama import credentials
from panorama import gst
from panorama import panorama_projections

class TestAppPlugin(gst_application.AppPlugin):
    def __init__(self):
        some python error
        gst_application.AppPlugin.__init__(self)

    def initialize(self, properties: properties.PropertyDelegate, credential_provider: credentials.CredentialProvider, event_broker: messagebroker.MessageBroker):
        pass

    def on_pipeline_error(self, error: gst.pipeline.PipelineError):
        pass

    def on_properties_changed(self, changed_properties: properties.PropertyCollection):
        pass

    def shutdown(self):
        pass

    def id(self):
        return "test_py_plugin"