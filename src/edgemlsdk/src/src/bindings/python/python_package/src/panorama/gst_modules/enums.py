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

from enum import Enum

class GstState(Enum):
    VOID_PENDING = 0
    NULL = 1
    READY = 2
    PAUSED = 3
    PLAYING = 4

class ErrorDomain(Enum):
    """
    Enumeration of Error Domains generated in GStreamer
    See https://gstreamer.freedesktop.org/documentation/gstreamer/gsterror.html?gi-language=c#enumerations for information
    """
    CORE = 0
    LIBRARY = 1
    RESOURCE = 2
    STREAM = 3
    NOT_DEFINED = 4
    UNKNOWN = 5