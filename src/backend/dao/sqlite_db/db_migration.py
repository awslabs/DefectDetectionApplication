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
# limitations under the License.i
import os
import json
import traceback
from marshmallow import ValidationError

from .workflow_dao import create_workflow
from .image_source_dao import create_image_source
from .image_source_configuration_dao import create_image_source_cfg
from .input_configuration_dao import create_input_source_cfg
from .output_configuration_dao import create_output_source_cfg
from .db_migration_utils import Old_WorkflowSchema
from model.image_source import ImageSourceSchema
from model.image_source_configuration import ImageSourceConfigurationSchema
from model.input_configuration import InputConfigurationSchema
from model.output_configuration import OutputConfigurationSchema
import logging
from .sqlite_db_operations import SessionLocal

logger = logging.getLogger(__name__)

class OldTinyDB():
    def __init__(self, jsonfile, create_op, schema):
        self.name = jsonfile.split('.')[0]
        self.filepath = os.path.join(os.environ['COMPONENT_WORK_PATH'], jsonfile)
        self.create = create_op
        self.schema = schema
        self.success = False
        self.logger = logger

    def is_migration_completed(self):
        return True if os.path.exists(self.filepath + '.tinydb') else False

    def read_db_files(self):
        if os.path.exists(self.filepath):
            logger.info(f"[LOADING FILE] {self.name}")
            if os.stat(self.filepath).st_size == 0:
                self.data = {}
            else:
                with open(self.filepath, "r") as db_file:
                    data = json.load(db_file)
                self.data = data["_default"]
            logger.info(f"[FILE LOADED] {len(self.data)} entry(s) found from TinyDB for {self.name}")
            return True
        else:
            logger.info(f"[FILE SKIPPED] {self.filepath} not found: TinyDB file for {self.name} does not exist")
            return False
        
    def save_cfg_as_id(self, mode, entry):
        if mode == "imagesource":
            if "imageSourceConfiguration" in entry:
                try:
                    entry["imageSourceConfigId"] = entry["imageSourceConfiguration"]["imageSourceConfigId"]
                except KeyError:
                    pass
                finally:
                    del entry["imageSourceConfiguration"]
        elif mode == "workflows":
            if "imageSources" in entry:
                entry["imageSourceId"] = entry["imageSources"][0]["imageSourceId"]
                del entry["imageSources"]
        return entry
    
    def migrate_entry(self, db, entry, mode):
        # cleanup: set null for the image capture path of Folder ImgSrc
        if mode == "imagesource":
            if entry.get("type") == "Folder":
                entry["imageCapturePath"] = None
        # cleanup: remove defaultConfigurations from db
        elif mode == "workflows":
            if entry.get("featureConfigurations"):
                if entry.get("featureConfigurations")[0].get("defaultConfiguration"):
                    del entry["featureConfigurations"][0]["defaultConfiguration"]
        try:
            data_model_loaded = self.schema.load(entry)
            data_dict = self.schema.dump(data_model_loaded)
        except ValidationError as ve:
            if str(ve) == "{'featureConfigurations': ['Missing data for required field.']}":
                data_dict = entry
            else:
                raise ve
        self.create(db, data_dict)

    def migrate_db(self):
        migrated_entries, skipped_entries = 0, 0
        logger.info(f"[MIGRATION STARTED]: {self.name}")
        try:
            with SessionLocal() as db:
                for entry in self.data.values():
                    try:
                        self.save_cfg_as_id(self.name, entry)
                        self.migrate_entry(db, entry, self.name)
                        migrated_entries += 1
                    except (ValidationError, KeyError) as err:
                        self.logger.error(f"[ENTRY SKIPPED] This entry is skipped due to some error: {err}. Entry: {entry}")
                        skipped_entries += 1
                        continue
        except:
            logger.error(f"[MIGRATION FAILED]: {self.name}")
            logger.error(traceback.format_exc())
            return self
        else:
            logger.info(f"[MIGRATION DONE]: {self.name} ({migrated_entries} entry(s) migrated, {skipped_entries} entry(s) skipped)")
            os.rename(self.filepath, self.filepath + '.tinydb')
            logger.info(f"[FILE RENAMED]: TinyDB file has been renamed as {self.filepath + '.tinydb'}")
            self.success = True
            return skipped_entries
        

workflow_db = OldTinyDB("workflows.json", create_workflow, Old_WorkflowSchema())
image_source_db = OldTinyDB("imagesource.json", create_image_source, ImageSourceSchema())
image_source_config_db = OldTinyDB("image_source_configs.json", create_image_source_cfg, ImageSourceConfigurationSchema())
input_config_db = OldTinyDB("inputconfigurations.json", create_input_source_cfg, InputConfigurationSchema())
output_config_db = OldTinyDB("outputconfigurations.json", create_output_source_cfg, OutputConfigurationSchema())
DB_list = [ workflow_db, image_source_db, image_source_config_db, input_config_db, output_config_db ]

def migrate():
    migrated_DB, skipped_DB, failed_DB, previous_migrated_DB = [], [], [], []
    skipped_entries_cnt = 0
    for tinyDB in DB_list:
        if not tinyDB.is_migration_completed():
            if tinyDB.read_db_files():
                result = tinyDB.migrate_db()
                if tinyDB.success:
                    skipped_entries_cnt += result
                    migrated_DB.append(tinyDB.name)
                else:
                    failed_DB.append(result)
            else:
                skipped_DB.append(tinyDB.name)
                continue
        else:
            previous_migrated_DB.append(tinyDB.name)
            continue

    if len(failed_DB):
        summary = "[MIGRATION FAILED]: "
        summary += f"{len(failed_DB)} db(s) failed, details can be found in log, "
    else:
        summary = "[MIGRATION SUCCEEDED]: "

    summary += f"{len(previous_migrated_DB)} db(s) migrated already and skipped, "
    summary += f"{len(migrated_DB)} db(s) migrated successfully, "
    if len(skipped_DB):
        summary += f"{len(skipped_DB)} db(s) skipped, JSON file not found, details can be found in log"
    if skipped_entries_cnt:
        summary += f"{skipped_entries_cnt} entry(s) skipped due to some error, details can be found in log"
    logger.info(summary)
    