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
from utils.utils import run_command


def chmod(path, mode, recursive=False):
    if recursive:
        return run_command([ 'chmod', '-R', mode, path ])
    return run_command([ 'chmod', mode, path ])


def chown(path, username, groupname=None, recursive=False):
    owner = username
    if groupname:
        owner += ":" + groupname
    if recursive:
        return run_command([ 'chown', '-R', owner, path ])
    return run_command([ 'chown', owner, path ])


def chgrp(path, groupname, recursive=False):
    if recursive:
        return run_command([ 'chgrp', '-R', groupname, path ])
    return run_command([ 'chgrp', groupname, path ])

