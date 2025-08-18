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

import ctypes
from ctypes import pythonapi

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst

import typing

from panorama import panorama_projections
from panorama import trace
from panorama import apidefs
from panorama import application
from panorama import unknown
from panorama.gst_modules import enums

class PipelineError:
    def __init__(self, native):
        self._native = native
        trace.debug(f"Creating pipeline error [{hex(id(self))}]")

    def __del__(self):
        trace.debug(f"Deleting pipeline error [{hex(id(self))}]")
        self._native.Release()
        self._native = None

    def message(self):
        """
        :return: The message associated with the error
        :rtype: string
        """
        return self._native.ErrorMessage()

    def debug_info(self):
        """
        :return: Additional debug information associated with the error
        :rtype: string
        """
        return self._native.DebugInfo()

    def message_type(self):
        """
        :return: The error message type
        :rtype: int
        """
        return self._native.MessageType()

    def domain(self):
        """
        :return: The error domain
        :rtype: :class:`panoramasdkv2.gst.pipeline.ErrorDomain`
        """
        return enums.ErrorDomain(self._native.Domain())

    def domain_quark(self):
        """
        The orignal quark for the domain
        Common values for Domain and Code found here
        https://gstreamer.freedesktop.org/documentation/gstreamer/gsterror.html?gi-language=c

        :return: The domain quark
        :rtype: uint
        """
        return self._native.DomainQuark()

    def domain_as_string(self):
        """
        Gets the domain (GQuark) as a string

        :return: String representation of the domain
        :rtype: string
        """
        return self._native.DomainAsString()

    def code(self):
        """
        :return: Error code
        :rtype: int
        """
        return self._native.Code()  
    
    def element_name(self):
        """
        :return: Name of the element that generated the error
        :rtype: string
        """
        return self._native.ElementName()
    
    def element_factory(self):
        """
        :return: The factory of the element that generated the error
        :rtype: string
        """
        return self._native.ElementFactory()

class PipelineEventHandler(unknown.UnknownImpl, panorama_projections.IPipelineEventHandler):
    def __init__(self):
        panorama_projections.IPipelineEventHandler.__init__(self)
        unknown.UnknownImpl.__init__(self)
        self.uuid = "23F8159F-A412-45FB-8EC6-9F43FF77C447"
        trace.debug(f"Creating pipeline event handler [{hex(id(self))}]")
        self.state_change_callbacks = []
        self.error_callbacks = []

    def __del__(self):
        trace.debug(f"Deleting pipeline event handler [{hex(id(self))}]")

    def OnError(self, sender, error):
        for cb in self.error_callbacks:
            pipeline = apidefs.assign(sender, lambda x: Pipeline(x))
            err = apidefs.assign(error, lambda x: PipelineError(x))

            try:
                cb(pipeline, err)
            except Exception as e:
                trace.error(f"Exception raised in Pipeline::OnError callback : {e}")

    def OnStateChanged(self, sender, oldState, newState):
        for cb in self.state_change_callbacks:
            pipeline = apidefs.assign(sender, lambda x: Pipeline(x))
            try:
                cb(pipeline, enums.GstState(oldState), enums.GstState(newState))
            except Exception as e:
                trace.error(f"Exception raised in Pipeline::OnStateChanged callback : {e}")

    def OnRemovedFromPipeline(self, sender):
        # todo: Not implemented
        pass

class _PyGObject_Functions(ctypes.Structure):
    _fields_ = [
        ('pygobject_register_class',
            ctypes.PYFUNCTYPE(ctypes.c_void_p)),
        ('pygobject_register_wrapper',
            ctypes.PYFUNCTYPE(ctypes.c_void_p)),
        ('pygobject_lookup_class',
            ctypes.PYFUNCTYPE(ctypes.c_void_p)),
        ('pygobject_new',
            ctypes.PYFUNCTYPE(ctypes.py_object, ctypes.c_void_p)),
        ]

class Pipeline:
    def __init__(self, native):
        self._native = native
        self._handler = None

        pythonapi.PyCapsule_GetPointer.restype = ctypes.c_void_p
        pythonapi.PyCapsule_GetPointer.argtypes = [ctypes.py_object, ctypes.c_char_p]
        addr = pythonapi.PyCapsule_GetPointer(gi._gobject._PyGObject_API, b'gobject._PyGObject_API')
        self._api = _PyGObject_Functions.from_address(addr)

        trace.debug(f"Creating pipeline [{hex(id(self))}]")

    def __del__(self):
        trace.debug(f"Deleting pipeline [{hex(id(self))}]")
        self._native.Release()
        self._native = None
        self._handler = None

    def __create_handler__(self):
        if self._handler is None:
            self._handler = PipelineEventHandler()
            panorama_projections.PyObjectAddRef(self._handler)
            self._native.AddPipelineEventHandler(self._handler)

    def set_state(self, desired : enums.GstState, wait_for : enums.GstState):
        """
        Sets the state of the pipeline

        Arguments:
            desired: The desired state to transition too
            wait_for: The state to wait for before returning
        """
        apidefs.CHECKHR(self._native.SetState(desired.value, wait_for.value))

    def start(self):
        """
        Calls SetState on the pipeline with desired state = GST_STATE_PLAYING and wait_for_state = GST_STATE_READY
        """
        apidefs.CHECKHR(self._native.SetState(enums.GstState.PLAYING.value, enums.GstState.READY.value))

    def stop(self):
        """
        Sets the state on the pipeline to GST_STATE_NULL and waits for the pipeline to stop
        """
        apidefs.CHECKHR(self._native.Stop())

    def pause(self):
        """
        Sets the state on the pipeline to GST_STATE_PAUSED and wait for the pipeline to reach that state
        """
        apidefs.CHECKHR(self._native.Pause())

    def change_definition(self, new_definition: str):
        """
        Changes the definition of a pipeline.  If the pipeline is playing it will restart the pipeline

        Arguments:
            new_definition: The new pipeline definition
        """
        apidefs.CHECKHR(self._native.ChangeDefinition(new_definition))

    def restart(self):
        """
        Restarts the pipeline
        """
        apidefs.CHECKHR(self._native.Restart())

    def refresh(self):
        """
        If variables were defined in the pipeline definition this will refresh those variables and apply any changes
        """
        apidefs.CHECKHR(self._native.Refresh())

    def state(self):
        """
        Returns the state of the pipeline
        """
        gst_state_enum = enums.GstState(self._native.State())
        return gst_state_enum

    def id(self):
        """
        Returns The id of the pipeline
        """
        return self._native.Id()

    def definition(self):
        """
        Returns the definition used to create the pipeline
        """
        return self._native.Definition()
    
    def subscribe_state_change(self, callback: typing.Callable[["Pipeline", enums.GstState, enums.GstState], None]):
        """
        Subscribes to the state changes in the pipeline.  

        Arguments:
            cb: The method to invoke on state change.
        """
        self.__create_handler__()
        self._handler.state_change_callbacks.append(callback)

    def subscribe_errors(self, callback: typing.Callable[["Pipeline", PipelineError], None]):
        """
        Subscribes to error events in the pipeline

        Arguments:
            cb: The method to invoke on pipeline error
        """
        self.__create_handler__()
        self._handler.error_callbacks.append(callback)

    def get_pygobject(self):
        return self._api.pygobject_new(self._native.Element())

def create(id: str, definition: str, application: application.App):
    """
    Create the pipeline

    Arguments:
        id: Id to give assign to the pipeline
        definition: String that defines the pipeline
        application:  Object returned from application.create()
    """
    res = panorama_projections.CreatePipeline(id, definition, application.native_pointer())
    apidefs.CHECKHR(res[0])
    return apidefs.attach(res[1], lambda x: Pipeline(x))