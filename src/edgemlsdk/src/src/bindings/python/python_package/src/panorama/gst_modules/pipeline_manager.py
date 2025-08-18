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

import typing

from panorama import unknown
from panorama import panorama_projections
from panorama import trace
from panorama import apidefs
from panorama import application
from panorama.gst_modules import pipeline

class PipelineManagerEventHandler(unknown.UnknownImpl, panorama_projections.IPipelineManagerEventHandler):
    def __init__(self):
        panorama_projections.IPipelineManagerEventHandler.__init__(self)
        unknown.UnknownImpl.__init__(self)
        self.uuid = "5CB95088-3740-43AB-A043-3BB3A8E4724C"
        trace.debug(f"Creating pipeline manager event handler [{hex(id(self))}]")

        self.pipeline_added_preview_callbacks = []
        self.pipeline_added_callbacks = []

        self.pipeline_removed_preview_callbacks = []
        self.pipeline_removed_callbacks = []

        self.pipeline_definition_changed_preview_callbacks = []

    def OnPipelineAddPreview(self, definition):
        handled: bool = False
        for cb in self.pipeline_added_preview_callbacks:
            id = definition.Id()
            defin = definition.GetDefinition()

            user_handled = False
            try:
                user_handled = cb(id, defin)
            except Exception as e:
                trace.error(f"Exception raised in PipelineManager::OnPipelineAddPreview callback : {e}")

            handled = handled or user_handled

        return handled

    def OnPipelineRemovePreview(self, pipeline_to_remove):
        handled: bool = False
        for cb in self.pipeline_removed_preview_callbacks:
            to_remove = apidefs.assign(pipeline_to_remove, lambda x: pipeline.Pipeline(x))

            user_handled = False
            try:
                user_handled = cb(to_remove)
            except Exception as e:
                trace.error(f"Exception raised in PipelineManager::OnPipelineRemovePreview callback : {e}")

            handled = handled or user_handled

        return handled

    def OnPipelineAdded(self, added_pipeline):
        for cb in self.pipeline_added_callbacks:
            added = apidefs.assign(added_pipeline, lambda x: pipeline.Pipeline(x))
            try:
                cb(added)
            except Exception as e:
                trace.error(f"Exception raised in PipelineManager::OnPipelineAdded callback : {e}")

    def OnPipelineRemoved(self, removed_pipeline):
        for cb in self.pipeline_removed_callbacks:
            removed = apidefs.assign(removed_pipeline, lambda x: pipeline.Pipeline(x))
            try:
                cb(removed)
            except Exception as e:
                trace.error(f"Exception raised in PipelineManager::OnPipelineRemoved callback : {e}")

    def OnDefintionChangePreview(pipeline, new_definition):
        # todo: Implement
        return False

class PipelineManager:
    """
    Class that handles the lifecycle of multiple pipelines
    """
    def __init__(self, native):
        self._native = native
        self._handler = None
        trace.debug(f"Creating Pipeline Manager [{hex(id(self))}]")

    def __del__(self):
        trace.debug(f"Deleting Pipeline Manager [{hex(id(self))}]")
        self._native.Release()
        self._native = None
        self._handler = None

    def __create_handler__(self):
        if self._handler is None:
            self._handler = PipelineManagerEventHandler()
            panorama_projections.PyObjectAddRef(self._handler)
            self._native.AddPipelineManagerEventHandler(self._handler)

    def initialize(self):
        """
        Initializes the pipeline manager.  This involves creating any pipeline that found in the 'pipelines' property
        """
        apidefs.CHECKHR(self._native.Initialize())

    def start(self):
        """
        Call start on all managed pipelines
        """
        apidefs.CHECKHR(self._native.Start())

    def restart(self):
        """
        Call restart on all managed pipelines
        """
        apidefs.CHECKHR(self._native.Restart())

    def stop(self):
        """
        Call stop on all managed pipelines
        """
        apidefs.CHECKHR(self._native.Stop())

    def refresh(self):
        """
        Call refresh on all managed pipelines
        """
        apidefs.CHECKHR(self._native.Refresh())

    def subscribe_pipeline_added_preview(self, callback: typing.Callable[[str, str], bool]):
        """
        Subscribe to pipeline added preview events

        Arguments:
            callback:  Callback to invoke.  Return 'handled' flag.  False if you want the pipeline to be added, True if you do not.
        """
        self.__create_handler__()
        self._handler.pipeline_added_preview_callbacks.append(callback)

    def subscribe_pipeline_added(self, callback: typing.Callable[[pipeline.Pipeline], None]):
        """
        Subscribe to pipeline added event

        Arguments:
            callback:  Callback to invoke
        """
        self.__create_handler__()
        self._handler.pipeline_added_callbacks.append(callback)

    def subscribe_pipeline_removed_preview(self, callback: typing.Callable[[pipeline.Pipeline], bool]):
        """
        Subscribe to pipeline remove preview events

        Arguments:
            callback:  Callback to invoke.  Return 'handled' flag.  False if you want the pipeline to be removed, True if you do not.
        """
        self.__create_handler__()
        self._handler.pipeline_removed_preview_callbacks.append(callback)

    def subscribe_pipeline_removed(self, callback: typing.Callable[[pipeline.Pipeline], None]):
        """
        Subscribe to pipeline removed event

        Arguments:
            callback:  Callback to invoke
        """
        self.__create_handler__()
        self._handler.pipeline_removed_callbacks.append(callback)

    # todo: Project remaining methods

def create(application: application.App):
    """
    Create the pipeline manager object

    Arguments:
        application:  Object returned from application.create()
    """
    res = panorama_projections.CreatePipelineManager(application.native_pointer())
    apidefs.CHECKHR(res[0])
    return apidefs.attach(res[1], lambda x: PipelineManager(x))