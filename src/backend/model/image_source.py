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
import os

from enum import Enum
from marshmallow import fields, post_load, Schema, validate, validates_schema, ValidationError
import logging
logger = logging.getLogger(__name__)

class ExtendedEnum(Enum):

    @classmethod
    def list(cls):
        return list(map(lambda c: c.value, cls))


class ImageSourceType(ExtendedEnum):
    CAMERA = "Camera"
    FOLDER = "Folder"
    ICAM = "ICam"
    NVIDIA_CSI = "NvidiaCSI"


class ImageSource:

    def __init__(self, imageSourceId, name, type, creationTime, lastUpdateTime, imageCapturePath, cameraId=None,
                 location=None, description="", imageSourceConfigId=None, imageSourceConfiguration={}):
        self.imageSourceId = imageSourceId
        self.name = name
        self.type = type
        self.location = location
        self.cameraId = cameraId
        self.creationTime = creationTime
        self.lastUpdateTime = lastUpdateTime
        self.imageCapturePath = imageCapturePath
        self.description = description
        self.imageSourceConfigId = imageSourceConfigId
        self.imageSourceConfiguration = imageSourceConfiguration

    def get(self, attr_name, default=None):
        return getattr(self, attr_name, default)

    def set(self, attr_name, value):
        return setattr(self, attr_name, value)

    def add_image_source_config(self, imageSourceConfiguration):
        self.imageSourceConfiguration = imageSourceConfiguration

    def __repr__(self):
        return "<ImageSource(imageSourceId={self.imageSourceId!r})>".format(self=self)


# Image source config id stored in image source db entry
class ImageSourceConfigIdSchema(Schema):
    imageSourceConfigId = fields.Str(required=True)


class ImageSourceSchema(Schema):
    imageSourceId = fields.Str()
    name = fields.Str()
    type = fields.Str(validate=validate.OneOf(ImageSourceType.list()), required=True)
    location = fields.Str(required=False, allow_none=True)
    cameraId = fields.Str(required=False, allow_none=True)
    description = fields.Str(required=False)
    creationTime = fields.Int()
    lastUpdateTime = fields.Int()
    imageCapturePath = fields.Str()
    imageSourceConfigId = fields.Str(required=False)

    @validates_schema
    def validate_schema_for_type(self, data, **kwargs):
        # Dont do schema validation for partial validators.
        if kwargs.get('partial'):
            return
        if data.get('type') == ImageSourceType.FOLDER.value and 'location' not in data:
            raise ValidationError('location is required when image source type is Folder')
        elif data.get('type') == ImageSourceType.CAMERA.value:
            missing_params = []
            for required_value in ['cameraId', 'imageSourceConfigId', 'imageCapturePath']:
                if required_value not in data or not data[required_value]:
                    missing_params.append(required_value)
            if missing_params:
                raise ValidationError('{} required when image source type is Camera'.format(missing_params))

        elif data.get('type') == ImageSourceType.ICAM.value:
            missing_params = []
            for required_value in ['imageCapturePath']:
                if required_value not in data or not data[required_value]:
                    missing_params.append(required_value)
            if missing_params:
                raise ValidationError('{} required when image source type is Icam'.format(missing_params))

    @post_load
    def make_image_source(self, data, **kwargs):
        return ImageSource(**data)
