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

from dao.sqlite_db import workflow_metadata_dao
from model.workflow_metadata import WorkflowMetadataSchema
from sqlalchemy.orm import Session
from utils import utils

import logging
logger = logging.getLogger(__name__)

class WorkflowMetadataAccessor:
    def __init__(self):
        self.schema = WorkflowMetadataSchema()

    def create_workflow_metadata(self, db: Session, data):
        try:
            workflow_id = data["workflowId"]
            result = self.schema.load(data)

            workflow_metadata_dao.create_workflow_metadata(db, self.schema.dump(result))
            logger.info(f"Created new workflow metadata entry for workflow id: " + str(workflow_id))
            
            return getattr(result, "workflowId")
        except ValidationError as err:
            logger.error(err.messages)
            raise HTTPException(
                status_code=400,
                detail="Unable to create workflow metadata. Error: 'Failed to validate workflow metadata: {}'. Check workflow metadata and try again".format(
                    err.messages
                )
            )
        
    def update_workflow_metadata(self, db: Session, data):
        try:
            workflow_id = data["workflowId"]
            result = self.schema.load(data)

            workflow_metadata_dao.update_workflow_metadata(db, self.schema.dump(result))
            logger.info(f"Updated workflow metadata for workflow id: " + str(workflow_id))
            
            return getattr(result, "workflowId")
        except ValidationError as err:
            logger.error(err.messages)
            raise HTTPException(
                status_code=400,
                detail="Unable to update workflow metadata. Error: 'Failed to validate workflow metadata: {}'. Check workflow metadata and try again".format(
                    err.messages
                )
            )
    
    def get_workflow_metadata(self, db: Session, workflow_id: str):
        workflow_metdata = workflow_metadata_dao.get_workflow_metadata(db, workflow_id)
        if not workflow_metdata:
            raise HTTPException(
                status_code=404,
                detail=f"The server can't find the workflow metadata. Error: 'The workflow metadata with id {workflow_id} doesn't exist'. Check the workflow ID and try again.",
            )
        else:
            return self.schema.dump(workflow_metdata)
        
    def list_workflow_metadatas(self, db: Session):
        return workflow_metadata_dao.list_workflow_metadatas(db)