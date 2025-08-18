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
import subprocess
from datetime import datetime
from fastapi import HTTPException


def take_snapshot(stationName):
    # current date and time
    now = datetime.now()
    file = "snapshot-" + stationName + "-" + now.strftime("%Y-%m-%d-%H-%M-%S") + ".tar"
    path = "/aws_dda/system/" + file

    try:
        grepOut = subprocess.check_output(["sh", "/snapshot/snapshot.sh", path])
        return "snapshotfile/" + file + ".gz"
    except subprocess.CalledProcessError as grepexc:
        raise HTTPException(
            status_code=500,
            detail=f"The server can't get the snapshot file. Error Code: '{grepexc.returncode}'. Error Message: '{grepexc.output}'. Check error message and try again.",
        )
