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
import time

from dao.sqlite_db import output_configuration_dao
from model.output_configuration import OutputConfigurationSchema
from sqlalchemy.orm import Session
from utils import utils


import logging
logger = logging.getLogger(__name__)
class OutputConfigurationAccessor:
    def __init__(self):
        self.schema = OutputConfigurationSchema()

    def create_output_configuration(self, db: Session, data):
        try:
            output_config_id = utils.gen_uuid()
            data["outputConfigurationId"] = output_config_id

            current_ts = int(time.time() * 1000)
            data["creationTime"] = current_ts

            result = self.schema.load(data)
            doc_id = output_configuration_dao.create_output_source_cfg(db, self.schema.dump(result))
            logger.info("Stored output configuration with doc id:" + str(doc_id))
            return getattr(result, "outputConfigurationId")
        except ValidationError as err:
            logger.error(err.messages)
            raise HTTPException(
                status_code=400,
                detail="Unable to create output configuration. Error: 'Failed to validate output configuration: {}'. Check output configurations and try again".format(
                    err.messages
                ),
            )

    def list_output_configurations(self, db: Session) -> dict:
        return output_configuration_dao.list_output_cfgs(db)

