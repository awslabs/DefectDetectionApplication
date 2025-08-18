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
from awsiot.greengrasscoreipc.model import (
    QOS,
    PublishToIoTCoreRequest
)

TIMEOUT = 10

import logging
logger = logging.getLogger(__name__)

class PublishHandler:
    def __init__(self, ipc_client):
        self.ipc_client = ipc_client

    def publish_message(self, topic, message):
        try:
            qos = QOS.AT_LEAST_ONCE
            logger.info("Publish message: {} to topic: {}".format(message, topic))
            request = PublishToIoTCoreRequest()
            request.topic_name = topic
            request.payload = bytes(message, "utf-8")
            request.qos = qos
            operation = self.ipc_client.new_publish_to_iot_core()
            operation.activate(request)
            future_response = operation.get_response()
            future_response.result(TIMEOUT)
            logger.info("Publish message was successful")
        except Exception as e:
            logger.error("Exception occurred when publishing message: {}".format(e))
