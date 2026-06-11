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
from marshmallow import Schema, fields, post_load

class ImageSourceConfiguration:
    def __init__(self, imageSourceConfigId, gain, exposure, processingPipeline, creationTime, imageCrop=None, device=None, deviceName=None,
                 reverseX=None, reverseY=None, balanceWhiteAuto=None, pixelFormat=None, width=None, height=None, offsetX=None, offsetY=None):
        self.imageSourceConfigId = imageSourceConfigId
        self.gain = gain
        self.exposure = exposure
        self.processingPipeline = processingPipeline
        self.creationTime = creationTime
        self.imageCrop = imageCrop
        self.device = device
        self.deviceName = deviceName
        # Advanced GenICam controls (applied to the device at acquisition time).
        self.reverseX = reverseX
        self.reverseY = reverseY
        self.balanceWhiteAuto = balanceWhiteAuto
        self.pixelFormat = pixelFormat
        self.width = width
        self.height = height
        self.offsetX = offsetX
        self.offsetY = offsetY

    def get(self, attr_name, default=None):
        return getattr(self, attr_name, default)

    def __repr__(self):
        return "<ImageSourceConfiguration(imageSourceConfigId={self.imageSourceConfigId!r})>".format(self=self)

class ImageCropConfigSchema(Schema):
    top = fields.Int(required=True)
    bottom = fields.Int(required=True)
    left = fields.Int(required=True)
    right = fields.Int(required=True)

class ImageSourceConfigurationSchema(Schema):
    imageSourceConfigId = fields.Str(required=True)
    gain = fields.Int(required=True)
    exposure = fields.Int(required=True)
    processingPipeline = fields.Str(required=True)
    creationTime = fields.Int(required=True)
    imageCrop = fields.Nested(ImageCropConfigSchema(), required=False)
    device = fields.Str(required=False, allow_none=True)
    deviceName = fields.Str(required=False, allow_none=True)
    # Advanced GenICam controls (optional; applied to the device at acquisition).
    reverseX = fields.Bool(required=False, allow_none=True)
    reverseY = fields.Bool(required=False, allow_none=True)
    balanceWhiteAuto = fields.Str(required=False, allow_none=True)
    pixelFormat = fields.Str(required=False, allow_none=True)
    width = fields.Int(required=False, allow_none=True)
    height = fields.Int(required=False, allow_none=True)
    offsetX = fields.Int(required=False, allow_none=True)
    offsetY = fields.Int(required=False, allow_none=True)

    @post_load
    def make_source(self, data, **kwargs):
        return ImageSourceConfiguration(**data)