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
import os
import posixpath
import tempfile

from hypothesis import given, settings
from hypothesis import strategies as st
from local_server_base_test_case import LocalServerBaseTestCase
from unittest.mock import Mock, patch, call
from utils import constants

OK = (True, b"ok")
FAIL = (False, b"boom")


class TestUpdateDdaUserFilePermissions(LocalServerBaseTestCase):

    def test_chown_then_chmod_with_defaults(self):
        from utils import dda_user_management_utils as dda
        with patch("utils.filesystem_management_utils.chown", return_value=OK) as chown, \
                patch("utils.filesystem_management_utils.chmod", return_value=OK) as chmod:
            dda.update_dda_user_file_permissions("/aws_dda/foo")

        chown.assert_called_once_with("/aws_dda/foo", constants.DDA_ADMIN_USER, constants.DDA_ADMIN_GROUP)
        chmod.assert_called_once_with("/aws_dda/foo", "770")

    def test_custom_permissions_passed_through(self):
        from utils import dda_user_management_utils as dda
        with patch("utils.filesystem_management_utils.chown", return_value=OK), \
                patch("utils.filesystem_management_utils.chmod", return_value=OK) as chmod:
            dda.update_dda_user_file_permissions("/aws_dda/foo", permissions="700")
        chmod.assert_called_once_with("/aws_dda/foo", "700")

    def test_chown_failure_raises_and_skips_chmod(self):
        from utils import dda_user_management_utils as dda
        with patch("utils.filesystem_management_utils.chown", return_value=FAIL), \
                patch("utils.filesystem_management_utils.chmod", return_value=OK) as chmod:
            with self.assertRaises(Exception):
                dda.update_dda_user_file_permissions("/aws_dda/foo")
        chmod.assert_not_called()

    def test_chmod_failure_raises(self):
        from utils import dda_user_management_utils as dda
        with patch("utils.filesystem_management_utils.chown", return_value=OK), \
                patch("utils.filesystem_management_utils.chmod", return_value=FAIL):
            with self.assertRaises(Exception):
                dda.update_dda_user_file_permissions("/aws_dda/foo")


class TestSetupDdaUsersAndGroups(LocalServerBaseTestCase):

    def test_deletes_then_creates_both_users(self):
        from utils import dda_user_management_utils as dda
        env = {
            "DDA_SYSTEM_USER_ID": "1001",
            "DDA_SYSTEM_GROUP_ID": "1002",
            "DDA_ADMIN_USER_ID": "1003",
            "DDA_ADMIN_GROUP_ID": "1004",
        }
        with patch.dict(os.environ, env), \
                patch("utils.user_group_management_utils.delete_user_and_group", return_value=OK) as delete, \
                patch("utils.user_group_management_utils.create_user_and_group", return_value=OK) as create:
            dda.setup_dda_users_and_groups()

        # Existing users are removed first (sync), then recreated with host IDs.
        delete.assert_any_call(constants.DDA_SYSTEM_USER, constants.DDA_SYSTEM_GROUP)
        delete.assert_any_call(constants.DDA_ADMIN_USER, constants.DDA_ADMIN_GROUP)
        create.assert_any_call(constants.DDA_SYSTEM_USER, constants.DDA_SYSTEM_GROUP, "1001", "1002")
        create.assert_any_call(constants.DDA_ADMIN_USER, constants.DDA_ADMIN_GROUP, "1003", "1004")

    def test_raises_when_create_fails(self):
        from utils import dda_user_management_utils as dda
        with patch.dict(os.environ, {}, clear=False), \
                patch("utils.user_group_management_utils.delete_user_and_group", return_value=OK), \
                patch("utils.user_group_management_utils.create_user_and_group", return_value=FAIL):
            with self.assertRaises(Exception):
                dda.setup_dda_users_and_groups()


class TestGetAllParentDirectories(LocalServerBaseTestCase):

    def test_returns_root_to_leaf_chain(self):
        from utils import dda_user_management_utils as dda
        result = list(dda.get_all_parent_directories("/aws_dda/a/b"))
        self.assertEqual(result, ["/", "/aws_dda", "/aws_dda/a", "/aws_dda/a/b"])


def _module_os(exists):
    """A stand-in for ``dda_user_management_utils``'s own ``os`` name.

    These tests used to patch ``os.path.exists`` and ``os.makedirs``
    themselves, which replaces them for every thread: a background thread
    of the backend under test (the camera-registry sync saving its state
    under /aws_dda) then called the mock too, and a JP6 build gate failed
    with ``makedirs`` "called 2 times" (2026-10-01). Patching only this
    module's ``os`` keeps the tests' view of the calls to their own.
    """
    fake = Mock(name="os")
    fake.path.exists.return_value = exists
    return fake


class TestCreateDdaUserDirectory(LocalServerBaseTestCase):

    def test_creates_dir_and_sets_perms_excluding_root_and_slash(self):
        from utils import dda_user_management_utils as dda
        fake_os = _module_os(exists=False)
        with patch.object(dda, "os", fake_os), \
                patch("utils.dda_user_management_utils.update_dda_user_file_permissions") as update_perms:
            result = dda.create_dda_user_directory("/aws_dda/a/b")

        self.assertEqual(result, "/aws_dda/a/b")
        fake_os.makedirs.assert_called_once_with("/aws_dda/a/b")
        # "/" and DDA_ROOT_FOLDER (/aws_dda) are excluded; only deeper dirs get perms.
        updated = [c.args[0] for c in update_perms.call_args_list]
        self.assertEqual(updated, ["/aws_dda/a", "/aws_dda/a/b"])
        self.assertNotIn("/", updated)
        self.assertNotIn(constants.DDA_ROOT_FOLDER, updated)

    def test_skips_makedirs_when_exists(self):
        from utils import dda_user_management_utils as dda
        fake_os = _module_os(exists=True)
        with patch.object(dda, "os", fake_os), \
                patch("utils.dda_user_management_utils.update_dda_user_file_permissions"):
            dda.create_dda_user_directory("/aws_dda/a/b")
        fake_os.makedirs.assert_not_called()

    def test_makedirs_oserror_propagates(self):
        from utils import dda_user_management_utils as dda
        fake_os = _module_os(exists=False)
        fake_os.makedirs.side_effect = OSError("denied")
        with patch.object(dda, "os", fake_os), \
                patch("utils.dda_user_management_utils.update_dda_user_file_permissions"):
            with self.assertRaises(OSError):
                dda.create_dda_user_directory("/aws_dda/a/b")


# security-scan-remediation-high R7: the walk is confined below /aws_dda,
# outside /aws_dda/greengrass and /aws_dda/system (Requirement 7.2).
_REFUSED_PATHS = (
    "/etc/x",
    "/aws_dda/../etc",
    "/aws_dda/inference-results/../..",
    "/tmp/images",
    "/aws_dda/greengrass/v2/config",
    "/aws_dda/system",
    "aws_dda/images",
)


class TestConfinedDdaPermissionWalk(LocalServerBaseTestCase):

    def test_unconfined_paths_are_refused_before_any_change(self):
        from utils import dda_user_management_utils as dda
        for path in _REFUSED_PATHS:
            fake_os = _module_os(exists=False)
            with patch.object(dda, "os", fake_os), \
                    patch("utils.filesystem_management_utils.chown", return_value=OK) as chown, \
                    patch("utils.filesystem_management_utils.chmod", return_value=OK) as chmod:
                with self.assertRaises(ValueError, msg=path) as ctx:
                    dda.create_dda_user_directory(path)
            self.assertIn(repr(path), str(ctx.exception))
            fake_os.makedirs.assert_not_called()
            chown.assert_not_called()
            chmod.assert_not_called()

    def test_missing_path_keeps_the_type_error(self):
        from utils import dda_user_management_utils as dda
        for path in (None, ""):
            fake_os = _module_os(exists=False)
            with patch.object(dda, "os", fake_os), \
                    patch("utils.dda_user_management_utils.update_dda_user_file_permissions") as update:
                with self.assertRaises(TypeError, msg=repr(path)):
                    dda.create_dda_user_directory(path)
            fake_os.makedirs.assert_not_called()
            update.assert_not_called()

    def test_symlink_below_the_root_that_leads_out_is_refused(self):
        from utils import dda_user_management_utils as dda
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "aws_dda")
            os.makedirs(os.path.join(root, "captures"))
            os.makedirs(os.path.join(tmp, "outside"))
            os.symlink(os.path.join(tmp, "outside"), os.path.join(root, "images"))
            fake_os = _module_os(exists=False)
            with patch.object(constants, "DDA_ROOT_FOLDER", root), \
                    patch.object(dda, "os", fake_os), \
                    patch("utils.dda_user_management_utils.update_dda_user_file_permissions") as update:
                with self.assertRaises(ValueError):
                    dda.create_dda_user_directory(os.path.join(root, "images", "cam1"))
                fake_os.makedirs.assert_not_called()
                update.assert_not_called()
                # A real directory below the same root still gets the walk.
                target = os.path.join(root, "captures", "cam1")
                self.assertEqual(dda.create_dda_user_directory(target), target)
            real_root = os.path.realpath(root)
            self.assertEqual(
                [c.args[0] for c in update.call_args_list],
                [real_root + "/captures", real_root + "/captures/cam1"],
            )

    def test_valid_nested_path_gets_todays_walk(self):
        from utils import dda_user_management_utils as dda
        path = "/aws_dda/inference-results/wf-1/failed"
        # Today's walk: every parent except "/" and DDA_ROOT_FOLDER.
        expected = [p for p in dda.get_all_parent_directories(path)
                    if p not in ("/", constants.DDA_ROOT_FOLDER)]
        fake_os = _module_os(exists=False)
        with patch.object(dda, "os", fake_os), \
                patch("utils.dda_user_management_utils.update_dda_user_file_permissions") as update:
            self.assertEqual(dda.create_dda_user_directory(path), path)
        fake_os.makedirs.assert_called_once_with(path)
        self.assertEqual([c.args[0] for c in update.call_args_list], expected)

    def test_returned_path_is_the_given_string(self):
        from utils import dda_user_management_utils as dda
        fake_os = _module_os(exists=False)
        with patch.object(dda, "os", fake_os), \
                patch("utils.dda_user_management_utils.update_dda_user_file_permissions") as update:
            result = dda.create_dda_user_directory("/aws_dda/inference-results//wf-2/")
        # The stored workflowOutputPath keeps the caller's string; the resolved
        # path is what gets created and walked.
        self.assertEqual(result, "/aws_dda/inference-results//wf-2/")
        fake_os.makedirs.assert_called_once_with("/aws_dda/inference-results/wf-2")
        self.assertEqual([c.args[0] for c in update.call_args_list],
                         ["/aws_dda/inference-results", "/aws_dda/inference-results/wf-2"])

    def test_update_permissions_refuses_an_unconfined_path(self):
        from utils import dda_user_management_utils as dda
        with patch("utils.filesystem_management_utils.chown", return_value=OK) as chown, \
                patch("utils.filesystem_management_utils.chmod", return_value=OK) as chmod:
            for path in ("/etc/passwd", "/aws_dda", "/aws_dda/system/run.sh",
                         "/aws_dda/a/../../etc", "/aws_dda/greengrass"):
                with self.assertRaises(ValueError, msg=path):
                    dda.update_dda_user_file_permissions(path)
        chown.assert_not_called()
        chmod.assert_not_called()


def _within(path, top):
    return posixpath.commonpath([top, path]) == top


def _in_allowed_area(path):
    """The oracle: an absolute, normalized path strictly below the real
    /aws_dda and outside /aws_dda/greengrass and /aws_dda/system."""
    if not posixpath.isabs(path) or posixpath.normpath(path) != path:
        return False
    root = posixpath.realpath("/aws_dda")
    protected = [posixpath.realpath(p) for p in ("/aws_dda/greengrass", "/aws_dda/system")]
    return path != root and _within(path, root) and not any(_within(path, p) for p in protected)


_SEGMENT = st.sampled_from(
    ["", ".", "..", "aws_dda", "greengrass", "system", "v2", "images",
     "inference-results", "etc", "tmp", "x\x00"])
_PATHS = st.one_of(
    st.text(),
    st.lists(_SEGMENT, max_size=8).map("/".join),
    st.lists(_SEGMENT, max_size=8).map(lambda s: "/" + "/".join(s)),
    st.lists(_SEGMENT, max_size=6).map(lambda s: "/aws_dda/" + "/".join(s)),
)


class TestPropertyConfinedWalk(LocalServerBaseTestCase):

    # Feature: security-scan-remediation-high, Property 3: The DDA permission
    # walk stays inside its area. Validates: Requirements 7.2
    # Runs at Hypothesis's own default example count, taken from its built-in
    # "default" profile, because the conftest's fast profile lowers it to 25.
    @settings(max_examples=settings.get_profile("default").max_examples, deadline=None)
    @given(path=_PATHS)
    def test_property_the_walk_stays_inside_its_area(self, path):
        from utils import dda_user_management_utils as dda
        try:
            resolved = dda.confine_dda_path(path)
        except (TypeError, ValueError):
            resolved = None
        else:
            self.assertTrue(_in_allowed_area(resolved), resolved)
        fake_os = _module_os(exists=False)
        with patch.object(dda, "os", fake_os), \
                patch("utils.dda_user_management_utils.update_dda_user_file_permissions") as update:
            try:
                dda.create_dda_user_directory(path)
            except (TypeError, ValueError):
                pass
        walked = [c.args[0] for c in update.call_args_list]
        for walked_path in walked:
            self.assertTrue(_in_allowed_area(walked_path), walked_path)
        if resolved is None:
            self.assertEqual(walked, [])
            fake_os.makedirs.assert_not_called()
