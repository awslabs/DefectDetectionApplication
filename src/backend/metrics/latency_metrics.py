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
from sqlalchemy.orm import Session
from utils.server_setup import LatencyTimeAccessor

import logging
logger = logging.getLogger(__name__)

class LatencyMetrics(object):
    def __init__(self):
        self.latency_timestamps = {}
        self.latency_time_accessor = LatencyTimeAccessor()

    def add_timestamp(self, latencyType):
        timestamp = time.time()
        self.latency_timestamps[latencyType] = timestamp
        logger.info(f"Stored {latencyType} timestamp locally")
        return timestamp
    
    def get_timestamp(self, latencyType):
        return self.latency_timestamps.get(latencyType)

    def commit_timestamps(self, db: Session, inference_capture_id):
        latency_time_entries = []
        for latencyType in self.latency_timestamps:
            latency_time_entries.append({"inferenceCaptureId": inference_capture_id, "latencyType": latencyType, "timestamp": self.get_timestamp(latencyType)})

        if latency_time_entries:
            self.latency_time_accessor.store_latency_times(db, latency_time_entries)
