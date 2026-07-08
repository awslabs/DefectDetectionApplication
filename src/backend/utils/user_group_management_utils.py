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
import os
import re
import pwd
import grp
from utils.utils import run_command


# POSIX user/group name allowlist. A name must start with a lowercase letter or
# underscore and contain only lowercase letters, digits, underscores, and
# hyphens. This rejects option-injection operands (a leading '-' is impossible)
# as well as shell metacharacters, so a user-influenced username/groupname can
# never be interpreted by useradd/userdel/groupadd/groupdel/gpasswd as an option.
_POSIX_NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]*$")


def _require_posix_name(value, kind):
    """Allowlist-validate a user/group name; raise ValueError on rejection so a
    malicious (e.g. leading-'-') operand never reaches subprocess.run."""
    if not isinstance(value, str) or not _POSIX_NAME_RE.match(value):
        raise ValueError(
            f"Invalid {kind} {value!r}: must match {_POSIX_NAME_RE.pattern} "
            f"(rejected to prevent option/command injection)"
        )
    return value


def is_user_exists(username):
    try:
        pwd.getpwnam(username)
        return True
    except KeyError:
        return False


def list_users():
    return [ user.pw_name for user in pwd.getpwall() ]


def get_username_from_id(userid):
    try:
        return pwd.getpwuid(userid).pw_name
    except KeyError:
        return 


def get_userid_from_name(username):
    try:
        return pwd.getpwnam(username).pw_uid
    except KeyError:
        return 


def create_user(username, groupname=None, userid=None):
    # Allowlist-validate the identity operands. useradd's argv here is
    # operand-first (LOGIN before the trailing --uid/-g options), so a '--'
    # end-of-options sentinel cannot be inserted without either reordering the
    # operands (changing the preserved argv) or pushing the options after '--'
    # (breaking useradd for legitimate callers). The allowlist is therefore the
    # neutralization for the option-injection operand: a leading '-' username is
    # rejected before it ever reaches subprocess.run.
    _require_posix_name(username, "username")
    __command = [ 'useradd', username ]
    if userid:
        __command += [ '--uid', userid ]
    if groupname:
        _require_posix_name(groupname, "groupname")
        __command += [ '-g', groupname ]
    return run_command(__command)


def create_user_if_not_exists(username, groupname=None, userid=None):
    if is_user_exists(username):
        return True, f"User {username} already exists"
    return create_user(username, groupname, userid)


def delete_user(username):
    # userdel's only operand is the login name; validate it and place it after a
    # '--' end-of-options sentinel so a leading '-' cannot be parsed as an option.
    _require_posix_name(username, "username")
    return run_command([ 'userdel', '--', username ])


def delete_user_if_exists(username):
    if not is_user_exists(username):
        return True, f"User {username} doesn't exist"
    return delete_user(username)


def create_user_and_group(username, groupname, userid=None, groupid=None):
    is_success, output = create_group_if_not_exists(groupname, groupid)
    if not is_success:
        return is_success, output
    return create_user_if_not_exists(username, groupname, userid)


def delete_user_and_group(username, groupname):
    is_success, output = delete_user_if_exists(username)
    if not is_success:
        return is_success, output
    return delete_group_if_exists(groupname)


def is_group_exists(groupname):
    try:
        grp.getgrnam(groupname)
        return True
    except KeyError:
        return False


def list_groups():
    return [ group.gr_name for group in grp.getgrall() ]


def get_groupname_from_id(groupid):
    try:
        return grp.getgrgid(groupid).gr_name
    except KeyError:
        return 


def get_groupid_from_name(groupname):
    try:
        return grp.getgrnam(groupname).gr_gid
    except KeyError:
        return 


def create_group(groupname, groupid=None):
    # groupadd's argv is operand-first (GROUP before the trailing --gid option),
    # so (as with create_user) a '--' sentinel cannot be added without breaking
    # the operand/option order; the allowlist neutralizes option injection by
    # rejecting a leading '-' groupname before it reaches subprocess.run.
    _require_posix_name(groupname, "groupname")
    __command = [ 'groupadd', groupname ]
    if groupid:
        __command += [ '--gid', groupid ]
    return run_command(__command)


def create_group_if_not_exists(groupname, groupid=None):
    if is_group_exists(groupname):
        return True, f"Group {groupname} already exists"
    return create_group(groupname, groupid)


def delete_group(groupname):
    # groupdel's only operand is the group name; validate it and place it after a
    # '--' end-of-options sentinel so a leading '-' cannot be parsed as an option.
    _require_posix_name(groupname, "groupname")
    return run_command([ 'groupdel', '--', groupname ])


def delete_group_if_exists(groupname):
    if not is_group_exists(groupname):
        return True, f"Group {groupname} doesn't exist"
    return delete_group(groupname)


def add_user_to_group(username, groupname):
    # gpasswd -a takes the username as the option-argument to -a and the group
    # as the trailing operand; because -a consumes the next token as its value,
    # a '--' sentinel cannot be cleanly interposed. Both identity operands are
    # allowlist-validated instead, so neither can be a leading-'-' option.
    _require_posix_name(username, "username")
    _require_posix_name(groupname, "groupname")
    return run_command([ 'gpasswd', '-a', username, groupname ])


def remove_user_from_group(username, groupname):
    _require_posix_name(username, "username")
    _require_posix_name(groupname, "groupname")
    return run_command([ 'gpasswd', '-d', username, groupname ])


def list_users_in_group(groupname):
    # Returns a list of all usernames that are a part of the input group
    try:
        return grp.getgrgid(groupname).gr_mem
    except KeyError:
        return 


def list_user_groups(username):
    # Returns a list of group names that an input username is a part of.
    try:
        groupid = get_groupid_from_name(username)
        linked_gids = os.getgrouplist(username, groupid)
        return [ get_groupname_from_id(groupid) for groupid in linked_gids ]
    except:
        return 


def is_user_in_group(username, groupname):
    # Check if an input user is a part of the group
    if username in list_users_in_group(groupname):
        return True
    return False
