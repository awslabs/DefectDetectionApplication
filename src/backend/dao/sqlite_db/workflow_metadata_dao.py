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
from sqlalchemy import select

from .models import WorkflowMetadata
from data_models.common import WorkflowMetadataModel

def get_workflow_metadata(db: Session, workflow_id: str):
    return db.get(WorkflowMetadata, workflow_id)

def create_workflow_metadata(db:Session, workflow_metadata: WorkflowMetadataModel):
    db_workflow_metadata = WorkflowMetadata(**workflow_metadata)
    db.add(db_workflow_metadata)
    db.commit()
    db.refresh(db_workflow_metadata)
    return db_workflow_metadata

def update_workflow_metadata(db:Session, workflow_metadata: WorkflowMetadataModel):
    workflow_metadata_entry = get_workflow_metadata(db, workflow_metadata['workflowId'])
    workflow_metadata_entry.summaryStartTime = workflow_metadata['summaryStartTime']
    db.commit()
    db.refresh(workflow_metadata_entry)
    return workflow_metadata_entry

def list_workflow_metadatas(db:Session):
    return db.scalars(select(WorkflowMetadata)).all()