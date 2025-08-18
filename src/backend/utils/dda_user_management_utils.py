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
import sys
import logging
from pathlib import Path
from collections import OrderedDict

from utils import constants, user_group_management_utils, filesystem_management_utils

DEFAULT_DDA_USER_PERMISSION = "770"


import logging
logger = logging.getLogger(__name__)


def setup_dda_users_and_groups():
    def __create_user_and_group(user, group, uid, gid):
        is_success, output = user_group_management_utils.create_user_and_group(user, group, uid, gid)
        if not is_success:
            logger.error(f"Failed to create user {user} and group {group}: {output}")
            raise

    def __delete_user_and_group(user, group):
        is_success, output = user_group_management_utils.delete_user_and_group(user, group)
        if not is_success:
            logger.error(f"Failed to remove user {user} and group {group}: {output}")
            raise

    # delete users if they already exists, this helps make sure the container syncs users/groups each time it spawns
    __delete_user_and_group(constants.DDA_SYSTEM_USER, constants.DDA_SYSTEM_GROUP)
    __delete_user_and_group(constants.DDA_ADMIN_USER, constants.DDA_ADMIN_GROUP)

    # Get host user/group ids from environment variables
    DDA_SYSTEM_USER_ID = os.getenv('DDA_SYSTEM_USER_ID')
    DDA_SYSTEM_GROUP_ID = os.getenv('DDA_SYSTEM_GROUP_ID')
    DDA_ADMIN_USER_ID = os.getenv('DDA_ADMIN_USER_ID')
    DDA_ADMIN_GROUP_ID = os.getenv('DDA_ADMIN_GROUP_ID')

    # create dda user/groups in container with same ids as host
    __create_user_and_group(constants.DDA_SYSTEM_USER, constants.DDA_SYSTEM_GROUP, DDA_SYSTEM_USER_ID, DDA_SYSTEM_GROUP_ID)
    __create_user_and_group(constants.DDA_ADMIN_USER, constants.DDA_ADMIN_GROUP, DDA_ADMIN_USER_ID, DDA_ADMIN_GROUP_ID)


def update_dda_user_file_permissions(filepath, permissions=DEFAULT_DDA_USER_PERMISSION):
    is_success, output = filesystem_management_utils.chown(filepath, constants.DDA_ADMIN_USER, constants.DDA_ADMIN_GROUP)
    if not is_success:
        logger.error(f"Unable to update ownership for {filepath}: {output}")
        raise

    is_success, output = filesystem_management_utils.chmod(filepath, permissions)
    if not is_success:
        logger.error(f"Unable to update permissions for {filepath}: {output}")
        raise


def create_dda_user_directory(folder_path):
    logger.info(f"Creating directory: {folder_path}")
    try:
        if not os.path.exists(folder_path):
            os.makedirs(folder_path)
        # add permissions to all parent directories except DDA_ROOT_FOLDER
        for dir in get_all_parent_directories(folder_path):
            if dir not in ['/', constants.DDA_ROOT_FOLDER]:
                update_dda_user_file_permissions(dir)
    except OSError as error:
        logger.error(f"Cannot create directory: {error}")
        raise
    except TypeError as error:
        logger.error("Folder path is required")
        raise
    return folder_path


def get_all_parent_directories(path):
    path = Path(path)
    paths = OrderedDict()
    for subpath in reversed(path.parents):
        paths[subpath.as_posix()] = subpath
    paths[path.as_posix()] = path
    return paths.keys()
