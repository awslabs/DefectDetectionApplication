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
from local_server_base_test_case import LocalServerBaseTestCase
from unittest.mock import patch

# run_command is imported into the module namespace via
# `from utils.utils import run_command`, so patch it there.
RUN_CMD = "utils.user_group_management_utils.run_command"

OK = (True, b"ok")
FAIL = (False, b"boom")


class TestUserGroupExistenceChecks(LocalServerBaseTestCase):

    def test_is_user_exists_true(self):
        from utils import user_group_management_utils as ug
        with patch("pwd.getpwnam", return_value=object()):
            self.assertTrue(ug.is_user_exists("alice"))

    def test_is_user_exists_false(self):
        from utils import user_group_management_utils as ug
        with patch("pwd.getpwnam", side_effect=KeyError):
            self.assertFalse(ug.is_user_exists("nobody"))

    def test_is_group_exists_true(self):
        from utils import user_group_management_utils as ug
        with patch("grp.getgrnam", return_value=object()):
            self.assertTrue(ug.is_group_exists("staff"))

    def test_is_group_exists_false(self):
        from utils import user_group_management_utils as ug
        with patch("grp.getgrnam", side_effect=KeyError):
            self.assertFalse(ug.is_group_exists("missing"))

    def test_get_userid_from_name_missing_returns_none(self):
        from utils import user_group_management_utils as ug
        with patch("pwd.getpwnam", side_effect=KeyError):
            self.assertIsNone(ug.get_userid_from_name("nobody"))

    def test_get_username_from_id_missing_returns_none(self):
        from utils import user_group_management_utils as ug
        with patch("pwd.getpwuid", side_effect=KeyError):
            self.assertIsNone(ug.get_username_from_id(99999))


class TestUserGroupCommandConstruction(LocalServerBaseTestCase):

    def test_create_user_minimal_command(self):
        from utils import user_group_management_utils as ug
        with patch(RUN_CMD, return_value=OK) as run:
            ug.create_user("alice")
        run.assert_called_once_with(["useradd", "alice"])

    def test_create_user_with_uid_and_group(self):
        from utils import user_group_management_utils as ug
        with patch(RUN_CMD, return_value=OK) as run:
            ug.create_user("alice", groupname="staff", userid="1500")
        run.assert_called_once_with(["useradd", "alice", "--uid", "1500", "-g", "staff"])

    def test_create_group_with_gid(self):
        from utils import user_group_management_utils as ug
        with patch(RUN_CMD, return_value=OK) as run:
            ug.create_group("staff", groupid="2000")
        run.assert_called_once_with(["groupadd", "staff", "--gid", "2000"])

    def test_delete_user_command(self):
        from utils import user_group_management_utils as ug
        with patch(RUN_CMD, return_value=OK) as run:
            ug.delete_user("alice")
        # The "--" end-of-options separator is an argument-injection hardening
        # (security batch #1-#8) so a login name starting with "-" cannot be
        # parsed as an option by userdel.
        run.assert_called_once_with(["userdel", "--", "alice"])

    def test_delete_group_command(self):
        from utils import user_group_management_utils as ug
        with patch(RUN_CMD, return_value=OK) as run:
            ug.delete_group("staff")
        # "--" end-of-options separator: hardened so a group name starting with
        # "-" cannot be parsed as an option by groupdel.
        run.assert_called_once_with(["groupdel", "--", "staff"])

    def test_add_user_to_group_command(self):
        from utils import user_group_management_utils as ug
        with patch(RUN_CMD, return_value=OK) as run:
            ug.add_user_to_group("alice", "staff")
        run.assert_called_once_with(["gpasswd", "-a", "alice", "staff"])

    def test_remove_user_from_group_command(self):
        from utils import user_group_management_utils as ug
        with patch(RUN_CMD, return_value=OK) as run:
            ug.remove_user_from_group("alice", "staff")
        run.assert_called_once_with(["gpasswd", "-d", "alice", "staff"])


class TestIdempotentHelpers(LocalServerBaseTestCase):

    def test_create_user_if_not_exists_short_circuits(self):
        from utils import user_group_management_utils as ug
        with patch("pwd.getpwnam", return_value=object()), patch(RUN_CMD) as run:
            is_success, msg = ug.create_user_if_not_exists("alice")
        self.assertTrue(is_success)
        self.assertIn("already exists", msg)
        run.assert_not_called()

    def test_create_user_if_not_exists_creates_when_absent(self):
        from utils import user_group_management_utils as ug
        with patch("pwd.getpwnam", side_effect=KeyError), patch(RUN_CMD, return_value=OK) as run:
            ug.create_user_if_not_exists("alice", groupname="staff", userid="1500")
        run.assert_called_once_with(["useradd", "alice", "--uid", "1500", "-g", "staff"])

    def test_create_group_if_not_exists_short_circuits(self):
        from utils import user_group_management_utils as ug
        with patch("grp.getgrnam", return_value=object()), patch(RUN_CMD) as run:
            is_success, msg = ug.create_group_if_not_exists("staff")
        self.assertTrue(is_success)
        self.assertIn("already exists", msg)
        run.assert_not_called()

    def test_delete_user_if_exists_short_circuits_when_absent(self):
        from utils import user_group_management_utils as ug
        with patch("pwd.getpwnam", side_effect=KeyError), patch(RUN_CMD) as run:
            is_success, msg = ug.delete_user_if_exists("nobody")
        self.assertTrue(is_success)
        self.assertIn("doesn't exist", msg)
        run.assert_not_called()

    def test_delete_group_if_exists_short_circuits_when_absent(self):
        from utils import user_group_management_utils as ug
        with patch("grp.getgrnam", side_effect=KeyError), patch(RUN_CMD) as run:
            is_success, msg = ug.delete_group_if_exists("missing")
        self.assertTrue(is_success)
        self.assertIn("doesn't exist", msg)
        run.assert_not_called()


class TestCreateDeleteUserAndGroup(LocalServerBaseTestCase):

    def test_create_user_and_group_aborts_if_group_fails(self):
        from utils import user_group_management_utils as ug
        # Group does not exist and groupadd fails -> user must NOT be created.
        with patch("grp.getgrnam", side_effect=KeyError), \
                patch("pwd.getpwnam", side_effect=KeyError), \
                patch(RUN_CMD, return_value=FAIL) as run:
            is_success, output = ug.create_user_and_group("alice", "staff", "1500", "2000")
        self.assertFalse(is_success)
        # Only the groupadd was attempted; useradd never ran.
        run.assert_called_once_with(["groupadd", "staff", "--gid", "2000"])

    def test_create_user_and_group_success_path(self):
        from utils import user_group_management_utils as ug
        with patch("grp.getgrnam", side_effect=KeyError), \
                patch("pwd.getpwnam", side_effect=KeyError), \
                patch(RUN_CMD, return_value=OK) as run:
            is_success, output = ug.create_user_and_group("alice", "staff", "1500", "2000")
        self.assertTrue(is_success)
        self.assertEqual(run.call_count, 2)
        run.assert_any_call(["groupadd", "staff", "--gid", "2000"])
        run.assert_any_call(["useradd", "alice", "--uid", "1500", "-g", "staff"])

    def test_delete_user_and_group_aborts_if_user_delete_fails(self):
        from utils import user_group_management_utils as ug
        # User exists, userdel fails -> group delete must NOT run.
        with patch("pwd.getpwnam", return_value=object()), \
                patch("grp.getgrnam", return_value=object()), \
                patch(RUN_CMD, return_value=FAIL) as run:
            is_success, output = ug.delete_user_and_group("alice", "staff")
        self.assertFalse(is_success)
        run.assert_called_once_with(["userdel", "--", "alice"])


class TestGroupMembership(LocalServerBaseTestCase):

    def test_is_user_in_group_true(self):
        from utils import user_group_management_utils as ug
        with patch.object(ug, "list_users_in_group", return_value=["alice", "bob"]):
            self.assertTrue(ug.is_user_in_group("alice", "staff"))

    def test_is_user_in_group_false(self):
        from utils import user_group_management_utils as ug
        with patch.object(ug, "list_users_in_group", return_value=["bob"]):
            self.assertFalse(ug.is_user_in_group("alice", "staff"))
