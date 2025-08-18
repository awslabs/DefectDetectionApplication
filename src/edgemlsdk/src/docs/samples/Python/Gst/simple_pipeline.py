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

import time
import json
from panorama import trace
from panorama import gst
from panorama import application
from panorama import messagebroker

def main():
    trace.add_console_trace_listener()

    # Create the app object
    app = application.create()

    # Initialize the Gstreamer framework
    gst.initialize()

    # Create the pipeline
    pipeline = gst.pipeline.create("my-pipeline", "videotestsrc ! ximagesink", app)

    # Start the pipeline
    pipeline.start()

    # Wait a few seconds a stop the pipeline.  The pipeline will automatically be stopped when the pipeline object is Garbage Collected if you haven't done it before then
    time.sleep(3)
    pipeline.stop()

    gst.shutdown()

if __name__ == "__main__":
    main()