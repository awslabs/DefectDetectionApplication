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

import awsiot.greengrasscoreipc.client as client
from awsiot.greengrasscoreipc.model import (
    IoTCoreMessage
)

from resources.accessors.workflow_accessor import WorkflowAccessor
from resources.accessors.workflow_metadata_accessor import WorkflowMetadataAccessor
from .ShadowUtils import remove_prefix, decode_shadow_payload
from model.StreamConfiguration import StreamConfigurationShadowSchema
from dao.sqlite_db.sqlite_db_operations import SessionLocal

import time
import logging

logger = logging.getLogger(__name__)

class CloudIoTShadowAccessor(client.SubscribeToIoTCoreStreamHandler):
    def __init__(self, topic_prefix, workflowAccessor: WorkflowAccessor, workflowMetadataAccessor: WorkflowMetadataAccessor):
        super().__init__()
        self.topic_prefix = topic_prefix
        self.workflowAccessor = workflowAccessor
        self.workflowMetadataAccessor = workflowMetadataAccessor
        self.message_dispatch = {"get/accepted": self._on_accepted,
                                 "update/accepted": self._on_accepted,
                                 "get/rejected": self._on_rejected,
                                 "update/rejected": self._on_rejected}

    def on_stream_event(self, event: IoTCoreMessage) -> None:
        try:
            topic_name = event.message.topic_name
            logger.info("Received message on topic {}".format(topic_name))
            subtopic = remove_prefix(topic_name, self.topic_prefix)
            if subtopic in self.message_dispatch:
                message = decode_shadow_payload(event.message.payload)
                self.message_dispatch[subtopic](subtopic, message)
            else:
                logger.info("Received payload on unsupported sub-topic: {}".format(subtopic))
        except Exception as e:
            logger.error("Error occurred: {}".format(e))

    def _on_accepted(self, subtopic, payload):
        logger.info("Received shadow document on sub-topic: {}, {}".format(subtopic, payload))
        shadow_schema = StreamConfigurationShadowSchema()
        workflows_shadow = shadow_schema.load(payload['state'], partial=True, unknown="exclude")

        # TODO: cloud still sends streamId instead of workflowId as the identifier.
        desired_workflow_ids = [workflow.streamId for workflow in workflows_shadow.desired.streams if workflow.enabled]

        with SessionLocal() as session:
            existing_workflow_ids = [workflow.workflowId for workflow in self.workflowAccessor.list_workflows(session)]

            # Delete undesired workflow IDs
            for existing_workflow_id in existing_workflow_ids:
                if existing_workflow_id not in desired_workflow_ids:
                    self.workflowAccessor.delete_workflow(existing_workflow_id, session)
                else:
                    desired_workflow_ids.remove(existing_workflow_id)

            # Add any new IDs that don't exist already.
            for desired_workflow_id in desired_workflow_ids:
                self.workflowAccessor.create_workflow({"workflowId": desired_workflow_id}, session)

        # Add workflow to workflow metadata table 
        with SessionLocal() as metadata_session:
            for desired_workflow_id in desired_workflow_ids:
                workflow_metadata_entry = {"workflowId": desired_workflow_id, "summaryStartTime": int(time.time())}
                self.workflowMetadataAccessor.create_workflow_metadata(metadata_session, workflow_metadata_entry)

    def _on_rejected(self, subtopic, payload):
        logger.info("Received failure in shadow sub-topic: {}, error: {}".format(subtopic, payload))

    def on_stream_error(self, error: Exception) -> bool:
        # Handle error.
        return True  # Return True to close stream, False to keep stream open.

    def on_stream_closed(self) -> None:
        # Handle close.
        pass
