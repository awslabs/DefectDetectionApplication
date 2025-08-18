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
import time

import awsiot.greengrasscoreipc
from awsiot.greengrasscoreipc.model import (
    QOS,
    SubscribeToIoTCoreRequest
)
from utils.server_setup import ipc_client

import logging
logger = logging.getLogger(__name__)

TIMEOUT = 10
SLEEP_TIME = 10
TOPIC_WILDCARD = "#"


class SubscriptionHandler:

    def __init__(self, topic_prefix, handler, publish_handler):
        self.operation = None
        self.topic_prefix = topic_prefix
        self.qos = QOS.AT_MOST_ONCE
        self.handler = handler
        self.publish_handler = publish_handler

    def subscribe(self):
        topic = self.topic_prefix + TOPIC_WILDCARD
        logger.info("Subscribing to topic {}".format(topic))
        request = SubscribeToIoTCoreRequest()
        request.topic_name = topic
        request.qos = self.qos
        self.operation = ipc_client.new_subscribe_to_iot_core(self.handler)
        self.operation.activate(request)
        future_response = self.operation.get_response()
        future_response.result(TIMEOUT)

        logger.info("Get current content from topic {}".format(topic))
        # Publish an empty message on shadow topic to retrive the current shadow document.
        # Ref https://docs.aws.amazon.com/iot/latest/developerguide/device-shadow-mqtt.html
        self.publish_handler.publish_message(self.topic_prefix + "get", "")

        # Keep the main thread alive, or the process will exit.
        while True:
            time.sleep(SLEEP_TIME)

    def close(self):
        # To stop subscribing, close the operation stream.
        logger.info("Closing MQTT connection")
        if self.operation:
            self.operation.close()
