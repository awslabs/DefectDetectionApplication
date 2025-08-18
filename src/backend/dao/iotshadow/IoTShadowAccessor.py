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

import json
import os

from awsiot.greengrasscoreipc.model import (
    GetThingShadowRequest,
    ListNamedShadowsForThingRequest,
    UpdateThingShadowRequest,
    ResourceNotFoundError
)

from model.PipelineConfiguration import PluginArg, PluginDefinition, PipelineConfiguration, \
    PipelineShadowObject, PipelineShadowStateConfiguration, PipelineShadow, \
    PipelineShadowObjectSchema, PipelineShadowStateConfigurationSchema, PipelineShadowSchema

from .ShadowUtils import decode_shadow_payload
import logging

TIMEOUT = 10
logger = logging.getLogger(__name__)

class IoTShadowAccessor:

    def __init__(self, ipc_client):
        self.ipc_client = ipc_client

    def list_named_shadows_for_thing_request(self, thing_name, next_token, page_size):
        try:

            # create the ListNamedShadowsForThingRequest request
            list_named_shadows_for_thing_request = ListNamedShadowsForThingRequest()
            list_named_shadows_for_thing_request.thing_name = thing_name
            list_named_shadows_for_thing_request.next_token = next_token
            list_named_shadows_for_thing_request.page_size = page_size

            # retrieve the ListNamedShadowsForThingRequest response after sending the request to the IPC server
            op = self.ipc_client.new_list_named_shadows_for_thing()
            op.activate(list_named_shadows_for_thing_request)
            fut = op.get_response()

            list_result = fut.result(TIMEOUT)

            # additional returned fields
            timestamp = list_result.timestamp
            next_token = list_result.next_token
            named_shadow_list = list_result.results

            return named_shadow_list, next_token, timestamp

        except Exception as e:
            # TODO: Raise exception
            logger.error("Exception occurred: {}".format(e))

    def get_thing_shadow_state_request(self, thing_name, shadow_name):
        try:
            # create the GetThingShadow request
            get_thing_shadow_request = GetThingShadowRequest()
            get_thing_shadow_request.thing_name = thing_name
            get_thing_shadow_request.shadow_name = shadow_name

            # retrieve the GetThingShadow response after sending the request to the IPC server
            op = self.ipc_client.new_get_thing_shadow()
            op.activate(get_thing_shadow_request)
            fut = op.get_response()

            result = fut.result(TIMEOUT)
            decoded_result = decode_shadow_payload(result.payload)
            return decoded_result["state"]
        except ResourceNotFoundError as e:
            logger.error("Shadow {} not found for thing {}".format(shadow_name, thing_name))
            return False
        except Exception as e:
            # TODO: Raise exception
            logger.error("Exception occurred: {}".format(e))

    def update_thing_shadow_state_request(self, thing_name, shadow_name, payload_str):
        try:
            logger.info("Updating {} shadow {} with payload: {}".format(thing_name, shadow_name, payload_str))
            payload = {"state": payload_str}
            # create the UpdateThingShadow request
            update_thing_shadow_request = UpdateThingShadowRequest()
            update_thing_shadow_request.thing_name = thing_name
            update_thing_shadow_request.shadow_name = shadow_name
            update_thing_shadow_request.payload = bytes(json.dumps(payload), "utf-8")

            # retrieve the UpdateThingShadow response after sending the request to the IPC server
            op = self.ipc_client.new_update_thing_shadow()
            op.activate(update_thing_shadow_request)
            fut = op.get_response()

            result = fut.result(TIMEOUT)
            logger.info("Update shadow result: {}".format(result))
            return result.payload

        except Exception as e:
            # TODO: Raise exception
            logger.error("Exception occurred: {}".format(e))
            raise e

    # Accessors specific to Pipeline shadow.
    def ensure_pipeline_shadow(self, thing_name, shadow_name):
        if not self.get_thing_shadow_state_request(thing_name, shadow_name):
            schema = PipelineShadowSchema(only=["desired"])
            payload_str = schema.dump(PipelineShadow(desired=PipelineShadowStateConfiguration([])))
            logger.info("Creating shadow {} with payload: {}".format(shadow_name, payload_str))
            result = self.update_thing_shadow_state_request(thing_name, shadow_name, payload_str)
            logger.info("Create shadow result: {}".format(result))
        else:
            logger.info("Shadow {} already exists".format(shadow_name))

    def store_pipeline_definition_in_shadow(self, thing_name, shadow_name, streamId, pipeline_definition):
        logger.info("Storing pipeline definition for stream id: {}, pipeline: {}"
                         .format(streamId, pipeline_definition))

        data = self.get_thing_shadow_state_request(os.environ['AWS_IOT_THING_NAME'],
                                                   os.environ["APPRUNNER_SHADOW_NAME"])
        logger.info("Current shadow: {}".format(data))
        schema = PipelineShadowSchema(only=["desired"])
        pipeline_shadow = schema.load(data, partial=True, unknown="exclude")
        pipeline_shadow.desired.upsert(PipelineShadowObject(streamId, pipeline_definition))
        payload_str = schema.dump(pipeline_shadow)
        logger.info("Updating shadow to: {}".format(payload_str))
        result = self.update_thing_shadow_state_request(thing_name, shadow_name, payload_str)
        return result

    def delete_pipeline_definition_in_shadow(self, thing_name, shadow_name, streamId):
        logger.info("Deleting pipeline definition for stream id: {}"
                         .format(streamId))

        data = self.get_thing_shadow_state_request(os.environ['AWS_IOT_THING_NAME'],
                                                   os.environ["APPRUNNER_SHADOW_NAME"])
        logger.info("Current shadow: {}".format(data))
        schema = PipelineShadowSchema(only=["desired"])
        pipeline_shadow = schema.load(data, partial=True, unknown="exclude")
        pipeline_shadow.desired.delete(streamId)
        payload_str = schema.dump(pipeline_shadow)
        logger.info("Updating shadow to: {}".format(payload_str))
        result = self.update_thing_shadow_state_request(thing_name, shadow_name, payload_str)
        return result