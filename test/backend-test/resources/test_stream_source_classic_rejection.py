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
"""Stream cameras cannot feed a classic workflow or a digital-input capture
(rtsp-rtmp-stream-cameras task 15.5 — Requirements 4.8, 18.2).

- Binding a classic (Pipeline_Configuration) workflow to an RTSP or RTMP
  Image_Source is rejected with the Requirement 4.8 message, and the
  workflow keeps its previous binding.
- The run path and both digital-input capture managers refuse a stream
  source that reached them anyway (a record from before the check),
  without touching the GStreamer executor.
- Folder sources behave as before.

Uses LocalServerBaseTestCase (the real app and database wiring), so it runs
in the flask-app image, like the other suites in this directory.
"""
import os
import shutil
import tempfile
from unittest.mock import Mock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from local_server_base_test_case import LocalServerBaseTestCase

RTSP_URL = "rtsp://10.0.4.21:554/Streaming/Channels/101"
RTMP_URL = "rtmp://media.local/live/line1"

folder_source = {
    "description": "folder source",
    "location": "/tmp/ddatests",
    "name": "folder-src",
    "type": "Folder",
}

feature_configuration = {"type": "LFVModel", "modelName": "LFVModelName"}


class TestStreamSourcesInClassicWorkflows(LocalServerBaseTestCase):
    def setUp(self):
        super().setUp()
        from model.stream_source import CLASSIC_PIPELINE_REJECTION
        from resources.accessors import image_source_accessor, workflow_accessor
        from stream_ingest import credentials, manager

        self.rejection = CLASSIC_PIPELINE_REJECTION
        self._credentials, self._manager = credentials, manager
        self._store_dir = tempfile.mkdtemp()
        credentials.set_credential_store(credentials.CredentialStore(
            directory=os.path.join(self._store_dir, "stream_credentials")))
        self.stream_manager = Mock(name="stream manager")
        self.stream_manager.health_for_image_source.return_value = None
        manager.set_stream_ingest_manager(self.stream_manager)

        self.session = Session(self.engine)
        self.image_source_accessor = image_source_accessor.ImageSourceAccessor()
        self.image_source_accessor._ImageSourceAccessor__create_folder = Mock()
        self.folder_id = self.image_source_accessor.create_image_source(
            dict(folder_source), self.session)["imageSourceId"]
        self.rtsp_id = self.image_source_accessor.create_image_source(
            {"name": "dock 3", "type": "RTSP", "location": RTSP_URL}, self.session)["imageSourceId"]
        self.rtmp_id = self.image_source_accessor.create_image_source(
            {"name": "line 1", "type": "RTMP", "location": RTMP_URL}, self.session)["imageSourceId"]

        self.workflow_accessor = workflow_accessor.WorkflowAccessor(Mock())
        self.workflow_accessor._WorkflowAccessor__create_folder = Mock(
            return_value="/aws_dda/inference-results/wf-stream")
        self.workflow_accessor.create_workflow({"workflowId": "wf-stream"}, self.session)

    def tearDown(self):
        self._credentials.set_credential_store(None)
        self._manager.set_stream_ingest_manager(None)
        self.session.close()
        shutil.rmtree(self._store_dir, ignore_errors=True)
        super().tearDown()

    def _bind(self, image_source_id):
        return self.workflow_accessor.update_workflow({
            "workflowId": "wf-stream",
            "name": "classic workflow",
            "imageSources": [{"imageSourceId": image_source_id}],
            "featureConfigurations": [feature_configuration],
        }, self.session)

    def _stored_image_source_id(self):
        from dao.sqlite_db.models import Workflow
        self.session.expire_all()
        return self.session.get(Workflow, "wf-stream").imageSourceId

    @patch('utils.utils.create_em_agent_config')
    def test_binding_a_classic_workflow_to_a_stream_source_is_rejected(self, create_agent_config):
        for image_source_id in (self.rtsp_id, self.rtmp_id):
            with pytest.raises(HTTPException) as raised:
                self._bind(image_source_id)
            self.assertEqual(raised.value.status_code, 400)
            self.assertIn(self.rejection, raised.value.detail)
        self.assertFalse(create_agent_config.called)
        self.assertIsNone(self._stored_image_source_id())

    @patch('os.path.getmtime', return_value=1)
    @patch('os.listdir', return_value=["test-1.jpg"])
    @patch('utils.utils.create_em_agent_config')
    def test_a_rejected_rebind_keeps_the_previous_source(self, create_agent_config, *_):
        self._bind(self.folder_id)
        self.assertEqual(self._stored_image_source_id(), self.folder_id)
        create_agent_config.reset_mock()

        with pytest.raises(HTTPException) as raised:
            self._bind(self.rtsp_id)

        self.assertIn(self.rejection, raised.value.detail)
        self.assertFalse(create_agent_config.called)
        self.assertEqual(self._stored_image_source_id(), self.folder_id)

    def test_the_run_path_refuses_a_stream_source(self):
        import endpoints.workflow as workflow_endpoints
        executor = Mock(name="gst_pipeline_executor")
        with patch.object(workflow_endpoints, "gst_pipeline_executor", executor):
            with pytest.raises(HTTPException) as raised:
                workflow_endpoints.configure_image_source_and_run_pipeline(
                    {"workflowId": "wf-stream", "imageSourceId": self.rtsp_id},
                    self.session, Mock(name="latency metrics"))
        self.assertEqual(raised.value.status_code, 400)
        self.assertEqual(raised.value.detail, self.rejection)
        self.assertFalse(executor.method_calls)

    def test_the_run_path_still_runs_a_folder_source(self):
        import endpoints.workflow as workflow_endpoints
        executor = Mock(name="gst_pipeline_executor")
        executor.execute_workflow_pipeline.return_value = ("capture-1", {})
        with patch.object(workflow_endpoints, "gst_pipeline_executor", executor):
            result = workflow_endpoints.configure_image_source_and_run_pipeline(
                {"workflowId": "wf-stream", "imageSourceId": self.folder_id},
                self.session, Mock(name="latency metrics"))
        self.assertEqual(result, ("capture-1", {}))
        self.assertTrue(executor.execute_workflow_pipeline.called)

    def _capture_manager(self, cls, source_type):
        manager = cls.__new__(cls)
        manager.workflow_id = "wf-stream"
        manager.image_source = {"type": source_type, "location": RTSP_URL}
        manager.gst_pipeline_executor = Mock(name="gst_pipeline_executor")
        manager.executor = Mock(name="thread pool")
        return manager

    def test_digital_input_capture_refuses_a_stream_source(self):
        from utils.digital_input_process_manager import DigitalInputProcess
        from utils.digital_input_thread_manager import DigitalInputThread
        for cls, error in ((DigitalInputThread, TypeError), (DigitalInputProcess, Exception)):
            for source_type in ("RTSP", "RTMP"):
                manager = self._capture_manager(cls, source_type)
                with pytest.raises(error) as raised:
                    manager.run_image_capture_pipeline(frame=None, prefix="trigger")
                self.assertEqual(str(raised.value), self.rejection)
                self.assertFalse(manager.gst_pipeline_executor.method_calls)
