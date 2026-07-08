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
import re
import subprocess
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException

# Allowlist for stationName: only alphanumerics, underscore, and hyphen. Any
# other character (including shell metacharacters and path separators) is
# rejected before the value is ever concatenated into a path or passed to the
# snapshot shell script.
_STATION_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")

# The single directory snapshots are allowed to live in.
_SNAPSHOT_DIR = Path("/aws_dda/system")


def take_snapshot(stationName):
    # Allowlist-validate stationName BEFORE constructing any path or invoking
    # subprocess so no shell metacharacter can reach the snapshot script.
    if not stationName or not _STATION_NAME_PATTERN.match(stationName):
        raise HTTPException(
            status_code=400,
            detail="Invalid stationName. Only letters, digits, '_' and '-' are allowed.",
        )

    # current date and time
    now = datetime.now()
    file = "snapshot-" + stationName + "-" + now.strftime("%Y-%m-%d-%H-%M-%S") + ".tar"

    # Defense-in-depth: resolve the full path and assert it stays directly
    # inside /aws_dda/system/, so no ../ or absolute-path escape is possible.
    resolved = (_SNAPSHOT_DIR / file).resolve()
    if resolved.parent != _SNAPSHOT_DIR:
        raise HTTPException(
            status_code=400,
            detail="Invalid stationName: resolved snapshot path escapes the snapshot directory.",
        )

    path = "/aws_dda/system/" + file

    try:
        grepOut = subprocess.check_output(["sh", "/snapshot/snapshot.sh", path])
        return "snapshotfile/" + file + ".gz"
    except subprocess.CalledProcessError as grepexc:
        raise HTTPException(
            status_code=500,
            detail=f"The server can't get the snapshot file. Error Code: '{grepexc.returncode}'. Error Message: '{grepexc.output}'. Check error message and try again.",
        )
