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

from .models import ImageSourceConfiguration
from data_models.common import ImageSourceConfigurationsOutputModel


def get_image_source_cfg(db: Session, image_source_configuration_id: str):
    return db.query(ImageSourceConfiguration).filter(ImageSourceConfiguration.imageSourceConfigId == image_source_configuration_id).first()


def list_image_source_cfgs(db: Session):
    return db.scalars(select(ImageSourceConfiguration)).all()


def create_image_source_cfg(db: Session, image_source_cfg: ImageSourceConfigurationsOutputModel):
    db_image_source_cfg = ImageSourceConfiguration(**image_source_cfg)
    db.add(db_image_source_cfg)
    db.commit()
    db.refresh(db_image_source_cfg)
    return db_image_source_cfg
