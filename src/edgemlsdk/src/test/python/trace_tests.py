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

from panorama import trace
import test_utils
#panorama.Trace.set_trace_level(panorama.TraceLevel.Warning)

class MyTraceListener(trace.TraceListener):
    def WriteMessage(self, level, timestamp, line, file, message):
        test_utils.Expect_Equal(trace.TraceLevel(level), trace.TraceLevel.Info)
        test_utils.Expect_Equal(message, "Hello World")
    
def set_log_level():
    level = trace.get_trace_level()

    trace.set_trace_level(trace.TraceLevel.Error)
    test_utils.Expect_Equal(trace.get_trace_level(), trace.TraceLevel.Error)

    trace.set_trace_level(trace.TraceLevel.Warning)
    test_utils.Expect_Equal(trace.get_trace_level(), trace.TraceLevel.Warning)

    trace.set_trace_level(trace.TraceLevel.Info)
    test_utils.Expect_Equal(trace.get_trace_level(), trace.TraceLevel.Info)

    trace.set_trace_level(trace.TraceLevel.Verbose)
    test_utils.Expect_Equal(trace.get_trace_level(), trace.TraceLevel.Verbose)

    trace.set_trace_level(trace.TraceLevel.Debug)
    test_utils.Expect_Equal(trace.get_trace_level(), trace.TraceLevel.Debug)

    trace.set_trace_level(level)

def log_listener():
    my_listener = MyTraceListener()
    test_utils.Expect_Success(trace.add_trace_listener(my_listener))
    trace.info("Hello World")
    test_utils.Expect_Success(trace.remove_trace_listener(my_listener))

def run_tests():
    set_log_level()
    log_listener()

run_tests()
