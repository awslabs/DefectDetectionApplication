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

from .models import ImageSource
from data_models.common import ImageSourceModel


def get_image_source(db: Session, image_source_id: str):
    image_source = db.get(ImageSource, image_source_id)
    # image_source.imageSourceConfiguration is needed to flush imageSourceConfiguration back
    # TODO: further investigation and remove below
    if image_source:
        image_source_cfg = image_source.imageSourceConfiguration
    return image_source


def list_image_sources(db: Session, type):
    if type is not None:
        return db.scalars(select(ImageSource).filter_by(type=type)).all()
    else:
        return db.scalars(select(ImageSource)).all()

def list_image_source_ids_by_camera(db:Session, camera_id: str):
    return db.scalars(select(ImageSource.imageSourceId).filter_by(cameraId=camera_id)).all()

def create_image_source(db: Session, image_source: ImageSourceModel):
    db_image_source = ImageSource(**image_source)
    db.add(db_image_source)
    db.commit()
    db.refresh(db_image_source)
    return db_image_source


def delete_image_source(db: Session, image_source_id: str):
    image_source = db.get(ImageSource, image_source_id)
    if not image_source:
        raise ValueError(f"Image Source with id {image_source_id} does not exist.")
    db.delete(image_source)
    db.commit()


def update_image_source(db: Session, image_source: ImageSourceModel, image_source_id: str):
    db_image_source = db.query(ImageSource).filter(ImageSource.imageSourceId == image_source_id)
    if not db_image_source:
        raise ValueError(f"Image Source with id {image_source_id} does not exist.")
    db_image_source.update(image_source)
    db.commit()
