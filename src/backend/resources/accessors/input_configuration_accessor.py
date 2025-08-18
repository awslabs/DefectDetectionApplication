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

from fastapi import HTTPException
from marshmallow import ValidationError
import time

from dao.sqlite_db import input_configuration_dao
from model.input_configuration import InputConfigurationSchema
from sqlalchemy.orm import Session
from utils import utils
import logging

logger = logging.getLogger(__name__)


class InputConfigurationAccessor:
    def __init__(self):
        self.schema = InputConfigurationSchema()

    def create_input_configuration(self, db: Session, data):
        try:
            input_config_id = utils.gen_uuid()
            data["inputConfigurationId"] = input_config_id

            current_ts = int(time.time() * 1000)
            data["creationTime"] = current_ts

            result = self.schema.load(data)
            doc_id = input_configuration_dao.create_input_source_cfg(db, self.schema.dump(result))
            logger.info("Stored input configuration with doc id:" + str(doc_id))
            return getattr(result, "inputConfigurationId")
        except ValidationError as err:
            logger.error(err.messages)
            raise HTTPException(
                status_code=400,
                detail="Server unable to create input configuration. Error: 'Failed to validate input configuration: {}'. Check input configurations and try again".format(
                    err.messages
                ),
            )

    def list_input_configurations(self, db: Session) -> dict:
        return input_configuration_dao.list_input_cfgs(db)
