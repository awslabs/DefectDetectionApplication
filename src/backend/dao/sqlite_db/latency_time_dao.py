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

from sqlalchemy.orm import Session
from sqlalchemy import select, insert

from .models import LatencyTime
from data_models.common import LatencyTimeModel

def get_latency_time(db: Session, inference_capture_id: str, latency_type: str):
    return db.get(LatencyTime, (inference_capture_id, latency_type))

def get_latency_times(db: Session, inference_capture_id: str):
    return db.scalars(select(LatencyTime).filter_by(inferenceCaptureId=inference_capture_id)).all()

def store_latency_time(db:Session, latency_time: LatencyTimeModel):
    db_latency_time = LatencyTime(**latency_time)
    db.add(db_latency_time)
    db.commit()
    db.refresh(db_latency_time)
    return db_latency_time

def store_latency_times(db: Session, latency_time_entries):
    db.execute(insert(LatencyTime), latency_time_entries)
    db.commit()