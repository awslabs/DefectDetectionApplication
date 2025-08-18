# Copyright 2025 Amazon Web Services, Inc.
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

import os, shutil


WORKFLOWS_TINYDB_JSON = '{ "1": { "workflowId": "zex05zj0", "name": "workflow_zex05zj0", "creationTime": 1697835991322, "lastUpdatedTime": 1697835991322, "workflowOutputPath": "/aws_dda/inference-results/zex05zj0" }, "2": { "workflowId": "nsunfnde", "name": "workflow_nsunfnde", "creationTime": 1697835991347, "lastUpdatedTime": 1697836886267, "workflowOutputPath": "/aws_dda/inference-results/nsunfnde", "inputConfigurations": [], "featureConfigurations": [ { "defaultConfiguration": { "modelAlias": "MCM-84042060" }, "modelName": "model-p63g3pfo", "type": "LFVModel" } ], "outputConfigurations": [], "imageSources": [ { "lastUpdateTime": 1697836875800, "creationTime": 1697836875800, "imageSourceConfiguration": {}, "description": "", "name": "asdfa", "type": "Folder", "imageCapturePath": "/aws_dda/image-capture/fldkklqe", "imageSourceId": "fldkklqe", "cameraId": null, "location": "/aws_dda/asdf" } ], "description": "" }, "3": { "workflowId": "e8gzvm8i", "name": "workflow_e8gzvm8i", "creationTime": 1697835991369, "lastUpdatedTime": 1697837827744, "workflowOutputPath": "/aws_dda/inference-results/e8gzvm8i", "inputConfigurations": [], "featureConfigurations": [ { "defaultConfiguration": { "modelAlias": "MCM-84042060" }, "modelName": "model-p63g3pfo", "type": "LFVModel" } ], "outputConfigurations": [ { "rule": "Anomaly", "signalType": "GPIO.RISING", "pulseWidth": 753, "creationTime": 1697837827747.0, "outputConfigurationId": "a5cvnse2", "pin": "3" } ], "imageSources": [ { "lastUpdateTime": 1697836906245, "creationTime": 1697836495699, "imageSourceConfiguration": { "imageSourceConfigId": "sc3cfglr" }, "description": "", "name": "Fake_1", "type": "Camera", "imageCapturePath": "/aws_dda/image-capture/ceonzmyn", "imageSourceId": "ceonzmyn", "cameraId": "Fake_1", "location": null } ], "description": "" } }'
WORKFLOWS_TINYDB_STR = '{ "_default": ' + WORKFLOWS_TINYDB_JSON + ' }'
# WORKFLOWS_SQLITE_JSON: replaced imageSourceConfiguration with imageSourceConfigId
WORKFLOWS_SQLITE_JSON = '{ "1": { "workflowId": "zex05zj0", "name": "workflow_zex05zj0", "creationTime": 1697835991322, "lastUpdatedTime": 1697835991322, "workflowOutputPath": "/aws_dda/inference-results/zex05zj0" }, "2": { "workflowId": "nsunfnde", "name": "workflow_nsunfnde", "creationTime": 1697835991347, "lastUpdatedTime": 1697836886267, "workflowOutputPath": "/aws_dda/inference-results/nsunfnde", "inputConfigurations": [], "featureConfigurations": [ { "defaultConfiguration": { "modelAlias": "MCM-84042060" }, "modelName": "model-p63g3pfo", "type": "LFVModel" } ], "outputConfigurations": [], "imageSourceId": "fldkklqe", "description": "" }, "3": { "workflowId": "e8gzvm8i", "name": "workflow_e8gzvm8i", "creationTime": 1697835991369, "lastUpdatedTime": 1697837827744, "workflowOutputPath": "/aws_dda/inference-results/e8gzvm8i", "inputConfigurations": [], "featureConfigurations": [ { "defaultConfiguration": { "modelAlias": "MCM-84042060" }, "modelName": "model-p63g3pfo", "type": "LFVModel" } ], "outputConfigurations": [ { "rule": "Anomaly", "signalType": "GPIO.RISING", "pulseWidth": 753, "creationTime": 1697837827747.0, "outputConfigurationId": "a5cvnse2", "pin": "3" } ], "imageSourceId": "ceonzmyn", "description": "" } }'

IMAGESOURCE_TINYDB_JSON = '{ "1": { "lastUpdateTime": 1697836906245, "creationTime": 1697836495699, "imageSourceConfiguration": { "imageSourceConfigId": "sc3cfglr" }, "description": "", "name": "Fake_1", "type": "Camera", "imageCapturePath": "/aws_dda/image-capture/ceonzmyn", "imageSourceId": "ceonzmyn", "cameraId": "Fake_1", "location": null }, "2": { "lastUpdateTime": 1697836863433, "creationTime": 1697836863433, "imageSourceConfiguration": { "imageSourceConfigId": "t74ti2vq" }, "description": "", "name": "Fake_1fasfads", "type": "Camera", "imageCapturePath": "/aws_dda/image-capture/7x2q7xt3", "imageSourceId": "7x2q7xt3", "cameraId": "Fake_1", "location": null }, "3": { "lastUpdateTime": 1697836875800, "creationTime": 1697836875800, "imageSourceConfiguration": {}, "description": "", "name": "asdfa", "type": "Folder", "imageCapturePath": "/aws_dda/image-capture/fldkklqe", "imageSourceId": "fldkklqe", "cameraId": null, "location": "/aws_dda/asdf" } }'
IMAGESOURCE_TINYDB_STR = '{ "_default": ' + IMAGESOURCE_TINYDB_JSON + ' }'

IMAGESOURCE_ONFIGURATION_TINYDB_STR = '{"_default": {"1": {"gain": 10, "imageSourceConfigId": "oobcm6t8", "exposure": 4000, "creationTime": 1697836495699, "processingPipeline": "capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert"}, "2": {"gain": 10, "imageSourceConfigId": "t74ti2vq", "exposure": 4000, "creationTime": 1697836863433, "processingPipeline": "capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert"}, "3": {"gain": 21, "imageSourceConfigId": "sc3cfglr", "exposure": 35698, "creationTime": 1697836906246, "processingPipeline": "capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert"}}}'
INPUT_CONFIGURATION_STR = ''
OUTPUT_CONFIGURATION_STR = '{"_default": {"1": {"rule": "Anomaly", "signalType": "GPIO.RISING", "pulseWidth": 753, "creationTime": 1697837827747.0, "outputConfigurationId": "a5cvnse2", "pin": "3"}}}'

mapping = {
    "workflows.json": WORKFLOWS_TINYDB_STR,
    "imagesource.json": IMAGESOURCE_TINYDB_STR,
    "image_source_configs.json": IMAGESOURCE_ONFIGURATION_TINYDB_STR,
    "inputconfigurations.json": INPUT_CONFIGURATION_STR,
    "outputconfigurations.json.tinydb": OUTPUT_CONFIGURATION_STR
}

def dummy_create(session, data_model):
    pass

def db_files_setup():
    db_files_dir = "test/backend-test/dao/sqlite_db/test_db_files"
    if os.path.exists(db_files_dir):
        shutil.rmtree(db_files_dir)
    os.mkdir(db_files_dir)
    for filename, content in mapping.items():
        with open(os.path.join(db_files_dir, filename), "w") as f:
            f.write(content)

def db_files_teardown():
    db_files_dir = "test/backend-test/dao/sqlite_db/test_db_files"
    if os.path.exists(db_files_dir):
        shutil.rmtree(db_files_dir)