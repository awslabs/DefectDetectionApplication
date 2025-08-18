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
from marshmallow import ValidationError
from fastapi import HTTPException
import time

from dao.sqlite_db import image_source_configuration_dao
from model.image_source_configuration import ImageSourceConfigurationSchema
from sqlalchemy.orm import Session
from utils import utils

import logging
logger = logging.getLogger(__name__)

class ImageSourceConfigurationAccessor:
    def __init__(self):
        self.schema = ImageSourceConfigurationSchema()

    def create_image_source_configuration(self, db: Session, data: dict) -> str:
        """
        Create an image source configuration.

        :param data: The image source configuration.
        :return: The created image source configuration.
        """
        try:
            image_src_cfg_id = utils.gen_uuid()
            data["imageSourceConfigId"] = image_src_cfg_id

            current_ts = int(time.time() * 1000)
            data["creationTime"] = current_ts
            result = self.schema.load(data)

            image_source_configuration_dao.create_image_source_cfg(db, self.schema.dump(result))
            logger.info("Stored image source cfg with id:" + str(image_src_cfg_id))

            return getattr(result, "imageSourceConfigId")

        except ValidationError as err:
            logger.error(err.messages)
            raise HTTPException(
                status_code=400,
                detail="Unable to create image source configuration. Error: 'Failed to validate image source configuration: {}'. Check image configurations and try again".format(
                    err.messages
                ),
            )

    def list_image_source_configurations(self, db: Session) -> dict:
        return image_source_configuration_dao.list_image_source_cfgs(db)

    def get_image_source_configuration(self, db: Session, image_source_config_id: str):
        return image_source_configuration_dao.get_image_source_cfg(db, image_source_configuration_id=image_source_config_id)
