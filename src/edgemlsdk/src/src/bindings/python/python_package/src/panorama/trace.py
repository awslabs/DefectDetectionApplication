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

import inspect
from enum import Enum

from panorama import panorama_projections
from panorama import unknown

class TraceLevel(Enum):
    """
    Enumeration of the SDKv2 Trace Levels
    """

    Error = 0
    """
    Traces all fatal errors.  Errors that do not allow the continuation of a procedure.  Program can still  recover if the error conditions are handled.
    """

    Warning = 1
    """
    Traces all warnings.  Non fatal errors/conditions that allow the continuation of a procedure, but the developer may want to be informed about it.
    """
    
    Info = 2
    """
    Traces all informational messages.  Events that happen once or infrequently, gives high level tracing of the progress of a procedure.
    """

    Verbose = 3
    """
    Traces all verbose messages.  Events that happen repeatedly, gives more detailed information about the progroess of a procedure but may pollute the logs with redudant and unecessary information.
    """
    
    Debug = 4
    """
    Traces all debug messages.  Traces all reference count changes and HttpCall request/response headers and bodies as well as any additional information that may be considered useful for debugging issues.
    """

def set_trace_level(level):
    """
    Sets the TraceLevel.

    Args:
        level (:class:`panoramasdkv2.trace.TraceLevel`): The trace level to set
    """
    panorama_projections.SetTraceLevel(level.value)

def get_trace_level():
    """
    Returns the current TraceLevel
    """
    return TraceLevel(panorama_projections.GetTraceLevel())


def add_console_trace_listener():
    """
    Adds the ConsoleTraceListener to the list of trace listeners.  This will route all trace messages to the console.
    """
    nativeTraceListener = panorama_projections.CreateConsoleTraceListener()
    if nativeTraceListener[0] == 0:
        panorama_projections.AddTraceListener(nativeTraceListener[1])

def add_trace_listener(listener):
    """
    Adds a custom trace listener to the list of listeners that will be notified when a trace statement is available

    Args:
        listener (:class:`panoramasdkv2.panorama_projections.ITraceListener`):  Implementation of the :class:`panoramasdkv2.panorama_projections.ITraceListener` interface
    """

    #todo: Add tests for add_trace_listener https://sim.amazon.com/issues/0c6bdc8f-7313-4f61-ac42-45740de87293
    panorama_projections.PyObjectAddRef(listener)
    return panorama_projections.AddTraceListener(listener)

def remove_trace_listener(listener):
    """
    Removes a custom trace listener to the list of listeners that will be notified when a trace statement is available

    Args:
        listener (:class:`panoramasdkv2.panorama_projections.ITraceListener`):  Instancee of the :class:`panoramasdkv2.panorama_projections.ITraceListener` interface to remove
    """

    #todo: Add tests for add_trace_listener https://sim.amazon.com/issues/0c6bdc8f-7313-4f61-ac42-45740de87293
    return panorama_projections.RemoveTraceListener(listener)

def error(message):
    """
    Trace a message with a :class:`panoramsdkv2.trace.TraceLevel` level of Error

    Args:
        message (string): The message to trace
    """
    panorama_projections.Trace(0, panorama_projections.NowAsTimestamp(), inspect.stack()[1].lineno, inspect.stack()[1].filename.split("/")[-1], message)

def warning(message):
    """
    Trace a message with a :class:`panoramsdkv2.trace.TraceLevel` level of Warning

    Args:
        message (string): The message to trace
    """
    panorama_projections.Trace(1, panorama_projections.NowAsTimestamp(), inspect.stack()[1].lineno, inspect.stack()[1].filename.split("/")[-1], message)

def info(message):
    """
    Trace a message with a :class:`panoramsdkv2.trace.TraceLevel` level of Info

    Args:
        message (string): The message to trace
    """
    panorama_projections.Trace(2, panorama_projections.NowAsTimestamp(), inspect.stack()[1].lineno, inspect.stack()[1].filename.split("/")[-1], message)

def verbose(message):
    """
    Trace a message with a :class:`panoramsdkv2.trace.TraceLevel` level of Verbose

    Args:
        message (string): The message to trace
    """
    panorama_projections.Trace(3, panorama_projections.NowAsTimestamp(), inspect.stack()[1].lineno, inspect.stack()[1].filename.split("/")[-1], message)

def debug(message):
    """
    Trace a message with a :class:`panoramsdkv2.trace.TraceLevel` level of Debug

    Args:
        message (string): The message to trace
    """
    panorama_projections.Trace(4, panorama_projections.NowAsTimestamp(), inspect.stack()[1].lineno, inspect.stack()[1].filename.split("/")[-1], message)

def log(level, message):
    """
    Trace a message 

    Args:
        level (:class:`panoramsdkv2.trace.TraceLevel`): The trace level
        message (string): The message to trace
    """
    panorama_projections.Trace(level, panorama_projections.NowAsTimestamp(), inspect.stack()[1].lineno, inspect.stack()[1].filename.split("/")[-1], message)


# Python implementation of ITraceListener
# Python <--> C
class TraceListener(unknown.UnknownImpl, panorama_projections.ITraceListener):
    def __init__(self):
        panorama_projections.ITraceListener.__init__(self)
        unknown.UnknownImpl.__init__(self)
        # must equal ITraceListener uuid
        self.uuid = "D7AFD57A-9A91-4E2E-831C-4B61D17467AD"

    def WriteMessage(self, level, timestamp, line, file, message):
        raise NotImplementedError()