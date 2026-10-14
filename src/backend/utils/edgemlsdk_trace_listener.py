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
import logging
logger = logging.getLogger(__name__)
from panorama import trace
from utils.edgemlsdk_trace_levels import is_per_call_trace
# Python implementation of ITraceListener
# Python <--> C
class EdgeMLSdkLoggingTraceListener(trace.TraceListener):
    def __init__(self):
        trace.TraceListener.__init__(self)

    def WriteMessage(self, level, timestamp, line, message_file, message):
        # file protocol is multithreaded, only one log should be sent.
        if level == trace.TraceLevel.Error.value:
            logger.error(f'[{message_file}:{line}] {message}')
        elif level == trace.TraceLevel.Warning.value:
            logger.warning(f'[{message_file}:{line}] {message}')
        elif level == trace.TraceLevel.Info.value:
            # Per-call chatter (a model status read on every buffer, a
            # result's confidence) goes to DEBUG; see
            # utils.edgemlsdk_trace_levels (rtsp-rtmp-stream-cameras
            # finding 18).
            if is_per_call_trace(message_file, message):
                logger.debug(f'[{message_file}:{line}] {message}')
            else:
                logger.info(f'[{message_file}:{line}] {message}')
        elif level == trace.TraceLevel.Verbose.value:
            logger.debug(f'[{message_file}:{line}] {message}')
