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

import logging

logger = logging.getLogger(__name__)

from panorama import messagebroker
from panorama import application


"""
Message Broker Config
Dynamically can understand patterns such as
workflow-path, workflow-id- provided from subscription locations, and gstreamer configs
capture_id is mapped to corelation id (c_id), via gstreamer setup.
"""
MESSAGE_BROKER_CONFIG = """
    {
        "targets": [{
                "protocol": "file",
                "name": "dda_file_results",
                "file_options": {}
            },
            {
                "protocol": "gpio",
                "name": "dda_gpio_results",
                "gpio_options": {}
            }
        ],
        "pipes": [{
                "message_id": "file-target_${workflow-path}-${ext}",
                "destinations": [{
                    "target_name": "dda_file_results",
                    "file_message_options": {
                        "directory": "${workflow-path}/",
                        "filename": "${c_id}.${ext}"
                    }
                }]
            },
            {
                "message_id": "gpio-target_${rules}_${signal_types}_${pins}_${pulse_width}",
                "destinations": [{
                    "target_name": "dda_gpio_results",
                    "gpio_message_options": {
                        "rules": "${rules}",
                        "signal_types": "${signal_types}",
                        "pins": "${pins}",
                        "pulse_width_ms": "${pulse_width}"
                    }
                }]
            }
        ]
    }
"""

class MessageBrokerClient:
    def __init__(self):
        # Message Broker
        messagebroker.set_default_config(MESSAGE_BROKER_CONFIG)
        self.app = application.create()
        self.broker = messagebroker.create(self.app,unique=True)
        self.broker.initialize()