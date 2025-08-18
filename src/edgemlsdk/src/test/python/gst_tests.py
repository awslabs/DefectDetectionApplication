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

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

import time
import os
import test_utils
import threading
import gc

from panorama import gst
from panorama import application
from panorama import messagebroker
from panorama import mlops
class PipelineTestClass:
    def __init__(self, app):
        self.app = app
        self.reached_state = threading.Event()
        self.waiting_for_state = gst.enums.GstState.VOID_PENDING
        self.error_evt = threading.Event()
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        pass

    # callbacks
    def state_change(self, pipeline, old, new):
        test_utils.Expect_Equal(pipeline.id(), "id")
        test_utils.Expect_Equal(pipeline.definition(), "videotestsrc ! fakesink")
        if new is self.waiting_for_state:
            self.reached_state.set()

    def error_occurred(self, pipeline, err):
        if self.error_evt.is_set() == False:
            test_utils.Expect_Equal(pipeline.id(), "id")
            test_utils.Expect_Equal(err.message_type(), 2)
            test_utils.Expect_Equal(err.domain(), gst.enums.ErrorDomain.RESOURCE)
            test_utils.Expect_Equal(err.message(), "Resource not found.")
            test_utils.Expect_Equal(err.element_name(), "myFileSrcPlugin")
            test_utils.Expect_Equal(err.element_factory(), "filesrc")
            self.error_evt.set()

    # tests
    def happy_path(self):
        pipeline = gst.pipeline.create("id", "videotestsrc ! fakesink", self.app)
        test_utils.Expect_Equal(pipeline.id(), "id")
        test_utils.Expect_Equal(pipeline.definition(), "videotestsrc ! fakesink")
        pipeline.subscribe_state_change(self.state_change)

        # start pipeline and wait for playing state
        self.waiting_for_state = gst.enums.GstState.PLAYING
        pipeline.start()
        test_utils.Expect_True(self.reached_state.wait(3))
        test_utils.Expect_Equal(pipeline.state(), gst.enums.GstState.PLAYING)

        pipeline.stop()
        test_utils.Expect_Equal(pipeline.state(), gst.enums.GstState.NULL)

    # tests
    def emltriton_test(self):
        broker = messagebroker.create(self.app)
        broker.initialize()
        model_dir = os.environ.get("MODEL_REPO_DIR","")
        triton_install_dir = os.environ.get("TRITON_INSTALL_DIR","")
        server =  mlops.create_triton_inference_server(model_dir, triton_install_dir)
        server.load_model("dynamic_model_2")
        time.sleep(2.0)
        output_name = "output_0"
        pipe_str = "videotestsrc num-buffers=1 ! capsfilter caps=video/x-raw,width=32,height=32,format=RGB ! " \
            "emltriton model=dynamic_model_2 model-repo=" + model_dir + " server-path=" + triton_install_dir + " ! " \
            "emlcapture interval=0 meta=triton_inference_"+  output_name +":results ! "\
            "fakesink"
        # Stress test starting and restarting pipeline
        for _ in range(1000):
            broker_set = threading.Event()
            def _set_inference_result(_):
                nonlocal broker_set
                broker_set.set()
            subscription_token = broker.subscribe("results", _set_inference_result)
            pipeline = gst.pipeline.create("id", pipe_str, self.app)
            # start pipeline and wait for playing state
            self.waiting_for_state = gst.enums.GstState.PLAYING
            pipeline.start()
            broker_set.wait(0.10)
            pipeline.stop()
            assert broker_set.is_set() == True
            broker.unsubscribe(subscription_token)
    # tests
    def emltriton_test_with_crop(self):
        broker = messagebroker.create(self.app)
        broker.initialize()
        model_dir = os.environ.get("MODEL_REPO_DIR","")
        triton_install_dir = os.environ.get("TRITON_INSTALL_DIR","")
        output_name = "output_0"
        server =  mlops.create_triton_inference_server(model_dir, triton_install_dir)
        server.load_model("dynamic_model_2")
        time.sleep(2.0)
        pipe_str = "videotestsrc num-buffers=1 ! capsfilter caps=video/x-raw,width=64,height=64,format=RGB ! videocrop top=16 bottom=16 left=16 right=16 ! capsfilter caps=video/x-raw,format=RGB ! " \
            "emltriton model=dynamic_model_2 model-repo=" + model_dir + " server-path=" + triton_install_dir + " ! " \
            "emlcapture interval=0 meta=triton_inference_"+  output_name +":results ! "\
            "fakesink"
        broker_set = threading.Event()
        def _set_inference_result(_):
            nonlocal broker_set
            broker_set.set()
        subscription_token = broker.subscribe("results", _set_inference_result)
        pipeline = gst.pipeline.create("id", pipe_str, self.app)
        # start pipeline and wait for playing state
        self.waiting_for_state = gst.enums.GstState.PLAYING
        pipeline.start()
        broker_set.wait(0.10)
        pipeline.stop()
        assert broker_set.is_set() == True
        broker.unsubscribe(subscription_token)

    def pipeline_error(self):
        pipeline = gst.pipeline.create("id", "filesrc location=/nonexistent/file name=myFileSrcPlugin ! decodebin ! fakesink", self.app)
        pipeline.subscribe_errors(self.error_occurred)

        test_utils.Expect_Exception(lambda: pipeline.start())
        test_utils.Expect_True(self.error_evt.wait(timeout=3))

        pipeline.stop()

    def pygobject(self):
        pipeline = gst.pipeline.create("id", "videotestsrc ! fakesink", self.app)
        pygobj = pipeline.get_pygobject()
        test_utils.Expect_True(pygobj is not None)

        pygobj.set_state(Gst.State.PLAYING)
        time.sleep(1)
        state = pygobj.get_state(Gst.CLOCK_TIME_NONE)
        test_utils.Expect_True(state.state == Gst.State.PLAYING)

class PipelineManagerTestClass:
    def __init__(self, app):
        self.app = app
        self.pipeline_added_preview_evt = threading.Event()
        self.pipeline_added_evt = threading.Event()
        self.pipeline_removed_preview_evt = threading.Event()
        self.pipeline_removed_evt = threading.Event()
        self.pipeline_state_changed_evt = threading.Event()
        self.foo = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.foo = None
        pass

    def pipeline_state_changed(self, pipeline, old_state, new_state):
        self.pipeline_state_changed_evt.set()

    def pipeline_added_preview(self, id, definition):
        test_utils.Expect_Equal(id, "id1")
        test_utils.Expect_Equal(definition, "videotestsrc ! fakesink")
        self.pipeline_added_preview_evt.set()
        return False

    def pipeline_added(self, pipeline):
        # This is here to validate that holding onto reference of pipeline in Python object doesn't screw up reference counting
        self.foo = pipeline
        test_utils.Expect_Equal(pipeline.id(), "id1")
        test_utils.Expect_Equal(pipeline.definition(), "videotestsrc ! fakesink")
        self.pipeline_added_evt.set()
        pipeline.subscribe_state_change(self.pipeline_state_changed)

    def pipeline_removed_preview(self, pipeline):
        test_utils.Expect_Equal(pipeline.id(), "id1")
        test_utils.Expect_Equal(pipeline.definition(), "videotestsrc ! fakesink")
        self.pipeline_removed_preview_evt.set()
        return False
    
    def pipeline_removed(self, pipeline):
        test_utils.Expect_Equal(pipeline.id(), "id1")
        test_utils.Expect_Equal(pipeline.definition(), "videotestsrc ! fakesink")
        self.pipeline_removed_evt.set()
    
    def happy_path(self):
        mgr = gst.pipeline_manager.create(self.app)
        mgr.subscribe_pipeline_added_preview(self.pipeline_added_preview)
        mgr.subscribe_pipeline_added(self.pipeline_added)
        mgr.subscribe_pipeline_removed_preview(self.pipeline_removed_preview)
        mgr.subscribe_pipeline_removed(self.pipeline_removed)

        mgr.initialize()
        mgr.start()

        # Validate the handler set one the pipeline object is invoked when generated from pipeline added
        test_utils.Expect_True(self.pipeline_state_changed_evt.wait(3))

        test_utils.Expect_True(self.pipeline_added_preview_evt.wait(0))
        test_utils.Expect_True(self.pipeline_added_evt.wait(0))

        property = self.app.get_property("pipelines")
        property.set_value("[]")

        mgr.refresh()
        test_utils.Expect_True(self.pipeline_removed_preview_evt.wait(0))
        test_utils.Expect_True(self.pipeline_removed_evt.wait(0))

def run_tests():
    app = application.create([b"--pipelines", b"[{\"id\":\"id1\", \"definition\": \"videotestsrc ! fakesink\"}]"])
    gst.initialize(2)
    
    with PipelineTestClass(app) as pipeline_test:
        pipeline_test.happy_path()
        pipeline_test.pipeline_error()
        pipeline_test.pygobject()
        pipeline_test.emltriton_test()
        pipeline_test.emltriton_test_with_crop()
    with PipelineManagerTestClass(app) as mgr_test:
        mgr_test.happy_path()

    gst.shutdown()

run_tests()