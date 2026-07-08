# Copyright 2025 Amazon Web Services, Inc.
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
"""#3 run_command callers preservation baseline (Req 3.3).

Spec: security-injection-deserialization-fixes — Property 2: Preservation.

The user-group and filesystem utilities build ``subprocess.run`` argument lists
and return ``run_command``'s ``(success, output)`` contract. The fix (task 5)
inserts a ``--`` end-of-options sentinel before user-influenced operands and
allowlist-validates identity operands. For LEGITIMATE operands the operand
VECTOR is semantically identical (post-``--`` tokens are the same operands) and
the ``(success, output)`` contract is unchanged.

Recorded F ``argv`` baselines (operand vectors — the exact current lists):
    create_user('u')                 -> ['useradd', 'u']
    create_user('u', groupname='g')  -> ['useradd', 'u', '-g', 'g']
    create_user('u', userid='1000')  -> ['useradd', 'u', '--uid', '1000']
    delete_user('u')                 -> ['userdel', 'u']
    create_group('g')                -> ['groupadd', 'g']
    create_group('g', groupid='7')   -> ['groupadd', 'g', '--gid', '7']
    delete_group('g')                -> ['groupdel', 'g']
    add_user_to_group('u','g')       -> ['gpasswd', '-a', 'u', 'g']
    remove_user_from_group('u','g')  -> ['gpasswd', '-d', 'u', 'g']
    chmod('/p','0644')               -> ['chmod', '0644', '/p']
    chmod('/p','0644', recursive)    -> ['chmod', '-R', '0644', '/p']
    chown('/p','u')                  -> ['chown', 'u', '/p']
    chown('/p','u','g')              -> ['chown', 'u:g', '/p']
    chown('/p','u', recursive)       -> ['chown', '-R', 'u', '/p']
    chown('/p','u','g', recursive)   -> ['chown', '-R', 'u:g', '/p']
    chgrp('/p','g')                  -> ['chgrp', 'g', '/p']
    chgrp('/p','g', recursive)       -> ['chgrp', '-R', 'g', '/p']

Preservation invariant: the design permits the fix to insert a ``--`` sentinel,
which is semantically identical for valid operands. So the assertion compares the
argv with any ``--`` end-of-options marker removed against the recorded baseline
(this holds on the UNFIXED tree — no ``--`` to strip — AND on the fixed tree —
``--`` stripped == same operands). The ``(success, output)`` return is asserted
to pass straight through from ``run_command`` unchanged.

These tests load the REAL caller modules with a stubbed ``utils.utils.run_command``
so task 13 re-runs them unchanged against the fixed source.

**Validates: Requirements 3.3**

Run:
    python3 -m pytest test/backend-test/security/preservation/test_preservation_run_command_callers.py \
        -p no:cacheprovider --noconftest -v
"""
import types

import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from _preservation_support import load_module_from_path

# The controllable (success, output) contract the stub returns; the callers must
# pass this through unchanged.
STUB_RETURN = (True, b"stub-stdout")


def _load_callers():
    """Load user_group_management_utils and filesystem_management_utils with a
    stubbed ``utils.utils.run_command`` that records argv and returns
    ``STUB_RETURN``. Returns (ug_module, fs_module, captured)."""
    captured = {"calls": []}

    utils_pkg = types.ModuleType("utils")
    utils_pkg.__path__ = []
    utils_utils = types.ModuleType("utils.utils")
    utils_utils.run_command = (
        lambda command: (captured["calls"].append(list(command)) or STUB_RETURN)
    )
    injected = {"utils": utils_pkg, "utils.utils": utils_utils}

    ug = load_module_from_path(
        "ug_preservation",
        "src/backend/utils/user_group_management_utils.py",
        injected_modules=injected,
    )
    fs = load_module_from_path(
        "fs_preservation",
        "src/backend/utils/filesystem_management_utils.py",
        injected_modules=injected,
    )
    return ug, fs, captured


def _strip_sentinel(argv):
    """Remove a single POSIX ``--`` end-of-options marker (the fix may insert
    one). For valid operands this yields the semantically identical operand
    vector on both the unfixed and fixed trees."""
    out = list(argv)
    if "--" in out:
        out.remove("--")
    return out


def _assert_argv(captured, expected_operands):
    argv = captured["calls"][-1]
    assert _strip_sentinel(argv) == expected_operands, (
        f"operand vector changed: got {argv!r}, expected operands {expected_operands!r}"
    )


# --------------------------------------------------------------------------- #
# Example baselines — exact recorded operand vectors + (success, output)
# --------------------------------------------------------------------------- #
# Validates: Requirements 3.3
def test_user_group_argv_baselines():
    ug, _fs, captured = _load_callers()

    assert ug.create_user("appuser") == STUB_RETURN
    _assert_argv(captured, ["useradd", "appuser"])

    ug.create_user("appuser", groupname="appgroup")
    _assert_argv(captured, ["useradd", "appuser", "-g", "appgroup"])

    ug.create_user("appuser", userid="1000")
    _assert_argv(captured, ["useradd", "appuser", "--uid", "1000"])

    ug.create_user("appuser", groupname="appgroup", userid="1000")
    _assert_argv(captured, ["useradd", "appuser", "--uid", "1000", "-g", "appgroup"])

    ug.delete_user("appuser")
    _assert_argv(captured, ["userdel", "appuser"])

    ug.create_group("appgroup")
    _assert_argv(captured, ["groupadd", "appgroup"])

    ug.create_group("appgroup", groupid="7")
    _assert_argv(captured, ["groupadd", "appgroup", "--gid", "7"])

    ug.delete_group("appgroup")
    _assert_argv(captured, ["groupdel", "appgroup"])

    ug.add_user_to_group("appuser", "appgroup")
    _assert_argv(captured, ["gpasswd", "-a", "appuser", "appgroup"])

    ug.remove_user_from_group("appuser", "appgroup")
    _assert_argv(captured, ["gpasswd", "-d", "appuser", "appgroup"])


# Validates: Requirements 3.3
def test_filesystem_argv_baselines():
    _ug, fs, captured = _load_callers()

    assert fs.chmod("/aws_dda/x", "0644") == STUB_RETURN
    _assert_argv(captured, ["chmod", "0644", "/aws_dda/x"])

    fs.chmod("/aws_dda/x", "0644", recursive=True)
    _assert_argv(captured, ["chmod", "-R", "0644", "/aws_dda/x"])

    fs.chown("/aws_dda/x", "appuser")
    _assert_argv(captured, ["chown", "appuser", "/aws_dda/x"])

    fs.chown("/aws_dda/x", "appuser", groupname="appgroup")
    _assert_argv(captured, ["chown", "appuser:appgroup", "/aws_dda/x"])

    fs.chown("/aws_dda/x", "appuser", recursive=True)
    _assert_argv(captured, ["chown", "-R", "appuser", "/aws_dda/x"])

    fs.chown("/aws_dda/x", "appuser", groupname="appgroup", recursive=True)
    _assert_argv(captured, ["chown", "-R", "appuser:appgroup", "/aws_dda/x"])

    fs.chgrp("/aws_dda/x", "appgroup")
    _assert_argv(captured, ["chgrp", "appgroup", "/aws_dda/x"])

    fs.chgrp("/aws_dda/x", "appgroup", recursive=True)
    _assert_argv(captured, ["chgrp", "-R", "appgroup", "/aws_dda/x"])


# Validates: Requirements 3.3
def test_success_output_contract_passthrough():
    """The callers return exactly run_command's (success, output) tuple."""
    ug, fs, _c = _load_callers()
    assert ug.create_user("appuser") == STUB_RETURN
    assert ug.delete_group("appgroup") == STUB_RETURN
    assert fs.chmod("/aws_dda/x", "0644") == STUB_RETURN
    assert fs.chgrp("/aws_dda/x", "appgroup", recursive=True) == STUB_RETURN


# --------------------------------------------------------------------------- #
# Property: valid identifiers/paths/modes preserve the operand vector + contract
# --------------------------------------------------------------------------- #
# Valid POSIX names (design allowlist ^[a-z_][a-z0-9_-]*$); no leading '-'.
_NAME = st.from_regex(r"\A[a-z_][a-z0-9_-]{0,20}\Z")
# A conservative valid path (absolute, no shell metacharacters, no leading '-').
_PATH = st.from_regex(r"\A/[A-Za-z0-9_./-]{1,30}\Z")
# Octal + simple symbolic chmod modes (no leading '-').
_MODE = st.one_of(
    st.from_regex(r"\A[0-7]{3,4}\Z"),
    st.sampled_from(["u+x", "g-w", "a+r", "u+rwx", "o-rwx"]),
)


# Validates: Requirements 3.3
@settings(max_examples=50, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(username=_NAME, groupname=_NAME)
def test_create_user_valid_names_preserve_argv_property(username, groupname):
    ug, _fs, captured = _load_callers()
    assert ug.create_user(username, groupname=groupname) == STUB_RETURN
    _assert_argv(captured, ["useradd", username, "-g", groupname])


# Validates: Requirements 3.3
@settings(max_examples=50, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(path=_PATH, mode=_MODE, recursive=st.booleans())
def test_chmod_valid_inputs_preserve_argv_property(path, mode, recursive):
    _ug, fs, captured = _load_callers()
    assert fs.chmod(path, mode, recursive=recursive) == STUB_RETURN
    expected = ["chmod", "-R", mode, path] if recursive else ["chmod", mode, path]
    _assert_argv(captured, expected)


# Validates: Requirements 3.3
@settings(max_examples=50, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(path=_PATH, username=_NAME, groupname=_NAME, recursive=st.booleans())
def test_chown_valid_inputs_preserve_argv_property(path, username, groupname, recursive):
    _ug, fs, captured = _load_callers()
    assert fs.chown(path, username, groupname=groupname, recursive=recursive) == STUB_RETURN
    owner = username + ":" + groupname
    expected = ["chown", "-R", owner, path] if recursive else ["chown", owner, path]
    _assert_argv(captured, expected)
