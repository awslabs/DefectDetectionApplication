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

from utils.utils import run_command


# POSIX user/group name allowlist (same as user_group_management_utils): starts
# with a lowercase letter or underscore, then lowercase letters/digits/_/-. A
# leading '-' is impossible, so a user-influenced owner/group cannot be parsed
# by chown/chgrp as an option.
_POSIX_NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]*$")

# chmod mode allowlist: an octal mode (3-4 digits) OR one or more comma-separated
# symbolic clauses (e.g. u+x, g-w, a+r, u+rwx, o-rwx, -x). Anything that is not a
# valid mode (e.g. '-R', '--reference=/etc/shadow') is rejected before it reaches
# subprocess.run.
_CHMOD_MODE_RE = re.compile(
    r"^([0-7]{3,4}|[ugoa]*[-+=][rwxXst]+(,[ugoa]*[-+=][rwxXst]+)*)$"
)


def _require_posix_name(value, kind):
    if not isinstance(value, str) or not _POSIX_NAME_RE.match(value):
        raise ValueError(
            f"Invalid {kind} {value!r}: must match {_POSIX_NAME_RE.pattern} "
            f"(rejected to prevent option/command injection)"
        )
    return value


def _require_chmod_mode(mode):
    if not isinstance(mode, str) or not _CHMOD_MODE_RE.match(mode):
        raise ValueError(
            f"Invalid chmod mode {mode!r}: must be an octal (e.g. 0644) or "
            f"symbolic (e.g. u+rwx) mode (rejected to prevent option injection)"
        )
    return mode


def chmod(path, mode, recursive=False):
    # Allowlist-validate the mode; the user-influenced path is placed after a
    # '--' end-of-options sentinel so a leading '-' path cannot be parsed as an
    # option. For valid inputs the operand vector is unchanged (the tool treats
    # the post-'--' token as the same path operand).
    _require_chmod_mode(mode)
    if recursive:
        return run_command([ 'chmod', '-R', mode, '--', path ])
    return run_command([ 'chmod', mode, '--', path ])


def chown(path, username, groupname=None, recursive=False):
    # Allowlist-validate the owner/group identity operands; place the path after
    # a '--' sentinel so it is always treated as an operand, not an option.
    _require_posix_name(username, "username")
    owner = username
    if groupname:
        _require_posix_name(groupname, "groupname")
        owner += ":" + groupname
    if recursive:
        return run_command([ 'chown', '-R', owner, '--', path ])
    return run_command([ 'chown', owner, '--', path ])


def chgrp(path, groupname, recursive=False):
    # Allowlist-validate the group operand; place the path after a '--' sentinel.
    _require_posix_name(groupname, "groupname")
    if recursive:
        return run_command([ 'chgrp', '-R', groupname, '--', path ])
    return run_command([ 'chgrp', groupname, '--', path ])

