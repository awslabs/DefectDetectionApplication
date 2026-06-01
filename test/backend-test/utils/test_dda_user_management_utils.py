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

from local_server_base_test_case import LocalServerBaseTestCase
from unittest.mock import patch, call
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


class TestCreateDdaUserDirectory(LocalServerBaseTestCase):

    def test_creates_dir_and_sets_perms_excluding_root_and_slash(self):
        from utils import dda_user_management_utils as dda
        with patch("utils.dda_user_management_utils.os.path.exists", return_value=False), \
                patch("utils.dda_user_management_utils.os.makedirs") as makedirs, \
                patch("utils.dda_user_management_utils.update_dda_user_file_permissions") as update_perms:
            result = dda.create_dda_user_directory("/aws_dda/a/b")

        self.assertEqual(result, "/aws_dda/a/b")
        makedirs.assert_called_once_with("/aws_dda/a/b")
        # "/" and DDA_ROOT_FOLDER (/aws_dda) are excluded; only deeper dirs get perms.
        updated = [c.args[0] for c in update_perms.call_args_list]
        self.assertEqual(updated, ["/aws_dda/a", "/aws_dda/a/b"])
        self.assertNotIn("/", updated)
        self.assertNotIn(constants.DDA_ROOT_FOLDER, updated)

    def test_skips_makedirs_when_exists(self):
        from utils import dda_user_management_utils as dda
        with patch("utils.dda_user_management_utils.os.path.exists", return_value=True), \
                patch("utils.dda_user_management_utils.os.makedirs") as makedirs, \
                patch("utils.dda_user_management_utils.update_dda_user_file_permissions"):
            dda.create_dda_user_directory("/aws_dda/a/b")
        makedirs.assert_not_called()

    def test_makedirs_oserror_propagates(self):
        from utils import dda_user_management_utils as dda
        with patch("utils.dda_user_management_utils.os.path.exists", return_value=False), \
                patch("utils.dda_user_management_utils.os.makedirs", side_effect=OSError("denied")), \
                patch("utils.dda_user_management_utils.update_dda_user_file_permissions"):
            with self.assertRaises(OSError):
                dda.create_dda_user_directory("/aws_dda/a/b")
