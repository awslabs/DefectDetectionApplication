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

import json
import time
from panorama import trace
from panorama import gst
from panorama import application
from panorama import properties

def create_properties_file1():
    data = {
        "pattern": 
        {
            "type": "string",
            "immutable": True,
            "value": "snow"
        },
        "pipelines":
        [
            {
                "id": "pipeline1",
                "definition": "videotestsrc name=src pattern=${pattern} ! ximagesink"
            },
            {
                "id": "pipeline2",
                "definition": "videotestsrc ! ximagesink"
            }
        ]
    }

    with open('./variables_sample.json', 'w') as file:
        json.dump(data, file, indent=4)
    
def create_properties_file2():
    data = {
        "pattern": 
        {
            "type": "string",
            "immutable": True,
            "value": "ball"
        },
        "pipelines":
        [
            {
                "id": "pipeline1",
                "definition": "videotestsrc name=src pattern=${pattern} ! ximagesink"
            }
        ]
    }

    with open('./variables_sample.json', 'w') as file:
        json.dump(data, file, indent=4)

def main():
    create_properties_file1()
    trace.add_console_trace_listener()

    # Create the file property delegate
    file_delegate = properties.create_file_property_delegate("./variables_sample.json")

    # Create the app object
    app = application.create()
    app.add_property_delegate(file_delegate)

    # Initialize the Gstreamer framework
    gst.initialize()

    # Create the pipeline
    mgr = gst.pipeline_manager.create(app)
    mgr.initialize()

    # Start the pipelines
    mgr.start()

    # Wait a few seconds and update the properties file. Since pattern is marked as immutable changing its value will cause the pipeline1 to restart.  pipeline2 was removed from the pipelines array so it will be stopped and removed from the manager
    time.sleep(3)
    create_properties_file2()

    # Synchronize the application to get changes to the properties
    app.synchronize()

    # Refresh the manager to keep the pipelines up to date with the current state of the properties
    mgr.refresh()
    
    # Wait a few seconds then shutdown
    time.sleep(3)
    mgr.stop()
    gst.shutdown()

if __name__ == "__main__":
    main()