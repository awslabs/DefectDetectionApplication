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
import os
import logging
import pytest
import json
import copy

import migration_utils

from unittest import TestCase, mock
from unittest.mock import patch
from marshmallow import ValidationError

from model.workflow import WorkflowSchema
from model.image_source import ImageSourceSchema
from model.image_source_configuration import ImageSourceConfigurationSchema
from model.input_configuration import InputConfigurationSchema
from model.output_configuration import OutputConfigurationSchema


@pytest.mark.usefixtures("caplog")
class TestDBMigrationScript(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env_patcher = patch.dict(os.environ, {"COMPONENT_WORK_PATH": "test/backend-test/dao/sqlite_db/test_db_files"})
        cls.env_patcher.start()
        migration_utils.db_files_setup()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.env_patcher.stop()
        migration_utils.db_files_teardown()

    @pytest.mark.usefixtures("caplog")
    def setUp(self):
        super().setUp()
        from dao.sqlite_db import db_migration
        from dao.sqlite_db.db_migration import OldTinyDB, migrate
        
        self.caplog.set_level(logging.INFO)
        self.root_path = "test/backend-test/dao/sqlite_db/test_db_files"
        self.create_op_patcher = patch("migration_utils.dummy_create")
        self.mock_create_op = self.create_op_patcher.start()

        self.workflows_db = OldTinyDB("workflows.json", self.mock_create_op, WorkflowSchema())
        self.image_source_db = OldTinyDB("imagesource.json", self.mock_create_op, ImageSourceSchema())
        self.image_source_config_db = OldTinyDB("image_source_configs.json", self.mock_create_op, ImageSourceConfigurationSchema())
        self.input_config_db = OldTinyDB("inputconfigurations.json", self.mock_create_op, InputConfigurationSchema())
        self.output_config_db = OldTinyDB("outputconfigurations.json", self.mock_create_op, OutputConfigurationSchema())

        from dao.sqlite_db.sqlite_db_operations import engine, Base, metadata_engine, BaseMetadata
        self.engine = engine
        self.metadata_engine = metadata_engine
        Base.metadata.create_all(self.engine)
        BaseMetadata.metadata.create_all(self.metadata_engine)

    def tearDown(self):
        super().tearDown()
        self.create_op_patcher.stop()

        from dao.sqlite_db.sqlite_db_operations import Base, BaseMetadata
        Base.metadata.drop_all(self.engine)
        BaseMetadata.metadata.drop_all(self.metadata_engine)

    def test_is_migration_completed_json(self):
        self.assertFalse(self.input_config_db.is_migration_completed())

    def test_is_migration_completed_jsontinydb(self):
        self.assertTrue(self.output_config_db.is_migration_completed())

    def test_read_db_files_json_empty(self):
        db = self.input_config_db
        self.assertTrue(db.read_db_files())
        self.assertEqual(db.data, {})
        log_msg = "[FILE LOADED] 0 entry(s) found from TinyDB for inputconfigurations"
        self.assertIn(log_msg, self.caplog.text)

    def test_read_db_files_json_valid(self):
        db = self.workflows_db
        self.assertTrue(db.read_db_files())
        self.assertEqual(db.data, json.loads(migration_utils.WORKFLOWS_TINYDB_JSON))
        log_msg = "[FILE LOADED] 3 entry(s) found from TinyDB for workflows"
        self.assertIn(log_msg, self.caplog.text)

    def test_read_db_files_jsontinydb(self):
        db = self.output_config_db
        self.assertFalse(db.read_db_files())
        log_msg = f"[FILE SKIPPED] {self.root_path+'/outputconfigurations.json'} not found: TinyDB file for outputconfigurations does not exist"
        self.assertIn(log_msg, self.caplog.text)

    def test_save_cfg_as_id_imagesource_valid(self):
        db = self.image_source_db
        entry = json.loads(migration_utils.IMAGESOURCE_TINYDB_JSON)["1"]
        self.assertIn("imageSourceConfiguration", entry.keys())
        self.assertNotIn("imageSourceConfigId", entry.keys())
        img_src_cfg_id = entry["imageSourceConfiguration"]["imageSourceConfigId"]

        db.save_cfg_as_id("imagesource", entry)
        self.assertNotIn("imageSourceConfiguration", entry.keys())
        self.assertIn("imageSourceConfigId", entry.keys())
        self.assertEqual(entry["imageSourceConfigId"], img_src_cfg_id)

    def test_save_cfg_as_id_imagesource_keyerror(self):
        db = self.image_source_db
        entry = json.loads(migration_utils.IMAGESOURCE_TINYDB_JSON)["3"]
        self.assertIn("imageSourceConfiguration", entry.keys())
        self.assertEqual(entry["imageSourceConfiguration"], {})
        
        try:
            db.save_cfg_as_id("imagesource", entry)
        except KeyError:
            assert False, "KeyError should be caught in OldTinyDB.save_cfg_as_id()"
        self.assertNotIn("imageSourceConfiguration", entry.keys())

    def test_save_cfg_as_id_workflows_valid(self):
        db = self.workflows_db
        entry = json.loads(migration_utils.WORKFLOWS_TINYDB_JSON)["2"]
        self.assertIn("imageSources", entry.keys())
        self.assertNotIn("imageSourceId", entry.keys())
        img_src_id = entry["imageSources"][0]["imageSourceId"]

        db.save_cfg_as_id("workflows", entry)
        self.assertIn("imageSourceId", entry.keys())
        self.assertNotIn("imageSources", entry.keys())
        self.assertEqual(entry["imageSourceId"], img_src_id)

    def test_save_cfg_as_id_workflows_no_imgsrc(self):
        db = self.workflows_db
        entry = json.loads(migration_utils.WORKFLOWS_TINYDB_JSON)["1"]
        entry_copy = copy.deepcopy(entry)
        self.assertNotIn("imageSources", entry.keys())

        db.save_cfg_as_id("workflows", entry)
        self.assertEqual(entry, entry_copy)

    def test_migrate_entry_workflows_valid(self):
        db = self.workflows_db
        entry = json.loads(migration_utils.WORKFLOWS_SQLITE_JSON)["2"]
        dummy_session = ""
        db.migrate_entry(dummy_session, entry, "workflows")
        self.mock_create_op.assert_called_with("", entry)

    def test_migrate_entry_workflows_validationerror(self):
        db = self.workflows_db
        entry = json.loads(migration_utils.WORKFLOWS_SQLITE_JSON)["2"]
        entry["unknowAttribute"] = ""
        dummy_session = []
        self.assertRaises(ValidationError, db.migrate_entry, dummy_session, entry, "workflows")
        self.mock_create_op.assert_not_called()
        
    @patch("dao.sqlite_db.db_migration.OldTinyDB.save_cfg_as_id", return_value=[])
    @patch("dao.sqlite_db.db_migration.os.rename", return_value=[])
    def test_migrate_db_valid(self, rename_op, save_cfg_as_id):
        db = self.workflows_db
        db.data = json.loads(migration_utils.WORKFLOWS_SQLITE_JSON)
        db.migrate_db()
        rename_op.assert_called_once()
        self.assertEqual(save_cfg_as_id.mock_calls, [mock.call("workflows", db.data[str(i)]) for i in range(1,4)])
        log_msg_1 = "[MIGRATION DONE]: workflows (3 entry(s) migrated, 0 entry(s) skipped)"
        log_msg_2 = "[FILE RENAMED]: TinyDB file has been renamed as test/backend-test/dao/sqlite_db/test_db_files/workflows.json.tinydb"
        self.assertIn(log_msg_1, self.caplog.text)
        self.assertIn(log_msg_2, self.caplog.text)

    @patch("dao.sqlite_db.db_migration.OldTinyDB.save_cfg_as_id", return_value=[])
    @patch("dao.sqlite_db.db_migration.os.rename", return_value=[])
    def test_migrate_db_validationError(self, rename_op, save_cfg_as_id):
        db = self.workflows_db
        db.data = json.loads(migration_utils.WORKFLOWS_SQLITE_JSON)
        db.data["1"]["unknowAttribute"] = ""
        db.migrate_db()
        rename_op.assert_called_once()
        self.assertEqual(save_cfg_as_id.mock_calls, [mock.call("workflows", db.data[str(i)]) for i in range(1,4)])
        log_msg_1 = "[MIGRATION DONE]: workflows (2 entry(s) migrated, 1 entry(s) skipped)"
        self.assertIn(log_msg_1, self.caplog.text)

    @patch("dao.sqlite_db.db_migration.OldTinyDB.save_cfg_as_id", side_effect=ValueError)
    @patch("dao.sqlite_db.db_migration.os.rename", return_value=[])
    def test_migrate_db_failed(self, rename_op, save_cfg_as_id):
        db = self.workflows_db
        db.data = json.loads(migration_utils.WORKFLOWS_SQLITE_JSON)
        db.data["1"]["unknowAttribute"] = ""
        db.migrate_db()
        rename_op.assert_not_called()
        self.assertEqual(save_cfg_as_id.mock_calls, [mock.call("workflows", db.data["1"])])
        log_msg = "[MIGRATION FAILED]: workflows"
        self.assertIn(log_msg, self.caplog.text)

    @patch("dao.sqlite_db.db_migration.OldTinyDB.migrate_db")
    @patch("dao.sqlite_db.db_migration.OldTinyDB.read_db_files")
    @patch("dao.sqlite_db.db_migration.OldTinyDB.is_migration_completed", return_value=True)
    def test_migrate_previous_completed(self, is_migration_completed, read_db_files, migrate_db):
        from dao.sqlite_db.db_migration import migrate
        migrate()
        self.assertEqual(len(is_migration_completed.mock_calls), 5)
        read_db_files.assert_not_called()
        migrate_db.assert_not_called()
        log_msg = "[MIGRATION SUCCEEDED]: "
        log_msg += "5 db(s) migrated already and skipped, 0 db(s) migrated successfully, "
        self.assertIn(log_msg, self.caplog.text)   

    @patch("dao.sqlite_db.db_migration.OldTinyDB.migrate_db", return_value=6)
    @patch("dao.sqlite_db.db_migration.OldTinyDB.read_db_files", return_value=True)
    @patch("dao.sqlite_db.db_migration.OldTinyDB.is_migration_completed", return_value=False)
    def test_migrate_failed(self, is_migration_completed, read_db_files, migrate_db):
        with patch("dao.sqlite_db.db_migration.workflow_db.success", True) as workflow_db:
            from dao.sqlite_db.db_migration import migrate
            migrate()
            self.assertEqual(len(is_migration_completed.mock_calls), 5)
            self.assertEqual(len(read_db_files.mock_calls), 5)
            self.assertEqual(len(migrate_db.mock_calls), 5)
            log_msg = "[MIGRATION FAILED]: "
            log_msg += "4 db(s) failed, details can be found in log, 0 db(s) migrated already and skipped, 1 db(s) migrated successfully, 6 entry(s) skipped due to some error, details can be found in log"
            self.assertIn(log_msg, self.caplog.text)   
            pass

    @patch("dao.sqlite_db.db_migration.OldTinyDB.migrate_db")
    @patch("dao.sqlite_db.db_migration.OldTinyDB.read_db_files", return_value=False)
    @patch("dao.sqlite_db.db_migration.OldTinyDB.is_migration_completed", return_value=False)
    def test_migrate_previous_skipped(self, is_migration_completed, read_db_files, migrate_db):
        from dao.sqlite_db.db_migration import migrate
        from dao.sqlite_db import db_migration
        migrate()
        self.assertEqual(len(is_migration_completed.mock_calls), 5)
        self.assertEqual(len(read_db_files.mock_calls), 5)
        migrate_db.assert_not_called()
        log_msg = "[MIGRATION SUCCEEDED]: "
        log_msg += "0 db(s) migrated already and skipped, 0 db(s) migrated successfully, 5 db(s) skipped, JSON file not found, details can be found in log"
        self.assertIn(log_msg, self.caplog.text)   
