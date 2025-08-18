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
from fastapi import HTTPException
from marshmallow import ValidationError

from dao.sqlite_db import latency_time_dao
from model.latency_time import LatencyTimeSchema
from sqlalchemy.orm import Session
from utils import utils

import logging
logger = logging.getLogger(__name__)

class LatencyTimeAccessor:
    def __init__(self):
        self.schema = LatencyTimeSchema()

    def store_latency_time(self, db: Session, data):
        try:
            inference_capture_id = data["inferenceCaptureId"]
            result = self.schema.load(data)

            latency_time_dao.store_latency_time(db, self.schema.dump(result))
            logger.info(f"Stored {data['latencyType']} time with inference capture id: " + str(inference_capture_id))
            
            return getattr(result, "inferenceCaptureId")
        except ValidationError as err:
            logger.error(err.messages)
            raise HTTPException(
                status_code=400,
                detail="Unable to create latency time. Error: 'Failed to validate latency time: {}'. Check latency times and try again".format(
                    err.messages
                )
            )
        
    def store_latency_times(self, db: Session, data_entries):
        try:
            latency_time_dao.store_latency_times(db, data_entries)
            logger.info(f"Bulk stored the following latency timestamps: {data_entries}")
        except ValidationError as err:
            logger.error(err.messages)
            raise HTTPException(
                status_code=400,
                detail="Unable to bulk store latency times. Error: 'Failed to validate latency time: {}'. Check latency times and try again".format(
                    err.messages
                )
            )
        
    def get_latency_time(self, db: Session, inference_capture_id: str, latency_type: str):
        latency_time = latency_time_dao.get_latency_time(db, inference_capture_id, latency_type)
        if not latency_time:
            logger.info(f"The server can't find the latency time. Inference capture id {inference_capture_id} with latency type {latency_type} doesn't exist.",)
            return None
        else:
            logger.info("Fetched latency time with inference capture id: " + inference_capture_id)
            latency_time_entry = self.schema.dump(latency_time)
            return latency_time_entry.get("timestamp")
        
    def get_latency_times(self, db: Session, inference_capture_id: str):
        latency_times = latency_time_dao.get_latency_times(db, inference_capture_id)
        return self.schema.dump(latency_times, many=True)