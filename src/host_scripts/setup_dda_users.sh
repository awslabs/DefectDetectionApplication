#!/bin/bash
#
#
# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
#

DDA_SYSTEM_GROUP="dda_system_group"
DDA_SYSTEM_USER="dda_system_user"
DDA_ADMIN_GROUP="dda_admin_group"
DDA_ADMIN_USER="dda_admin_user"
GGC_USER="ggc_user"
GGC_GROUP="ggc_group"
VIDEO_GROUP="video"

DDA_ROOT_DIR="/aws_dda"

## TODO:: Move the common functions to a separate utility script
##
function isUserExists(){
    if id "$1" >/dev/null 2>&1 ; then
        # user exists
        return 0
    fi
    return 1
}

function isGroupExists(){
    if [ $(getent group "$1") ]; then
        # group exists
        return 0
    fi
    return 1
}

function addUser(){
    local USERNAME=$1
    local GROUPNAME=$2
    if ! isUserExists $USERNAME; then
        useradd $USERNAME -g $GROUPNAME
    fi
}

function addGroup(){
    local GROUPNAME=$1
    #  Create DDA_SYSTEM_USER and DDA_SYSTEM_GROUP if not exists
    if ! isGroupExists $GROUPNAME; then
        groupadd $GROUPNAME
    fi
}

function removeUser(){
    local USERNAME=$1
    if isUserExists $USERNAME; then
        userdel $USERNAME
    fi
}

function removeGroup(){
    local GROUPNAME=$1
    #  Create DDA_SYSTEM_USER and DDA_SYSTEM_GROUP if not exists
    if isGroupExists $GROUPNAME; then
        groupdel $GROUPNAME
    fi
}

function addUserToGroup(){
    local USERNAME=$1
    local GROUPNAME=$2
    gpasswd -a $USERNAME $GROUPNAME
}

function getUserIdFromName(){
    local USERNAME=$1
    echo $(id -u $USERNAME)
}

function getGroupIdFromName(){
    local GROUPNAME=$1
    echo $(getent group $GROUPNAME | cut -d: -f3)
}

function validateUser() {
    local USERNAME=$1
    if ! isUserExists $USERNAME; then
        echo "Failed to locate $USERNAME on host."
        exit 1
    fi
}

function validateGroup() {
    local GROUPNAME=$1
    if ! isGroupExists $GROUPNAME; then
        echo "Failed to locate $GROUPNAME on host."
        exit 1
    fi
}

#### Main script begins
DEFAULT_USER=$(awk -F":" '/1000/ {print $1}' /etc/passwd)

# Create users and groups
if ! isGroupExists $DDA_SYSTEM_GROUP; then
    addGroup $DDA_SYSTEM_GROUP
fi

if ! isGroupExists $DDA_ADMIN_GROUP; then
    addGroup $DDA_ADMIN_GROUP
    # add default user to admin group
    addUserToGroup $DEFAULT_USER $DDA_ADMIN_GROUP
fi

if ! isUserExists $DDA_ADMIN_USER; then
    addUser $DDA_ADMIN_USER $DDA_ADMIN_GROUP
fi

if ! isUserExists $DDA_SYSTEM_USER; then
    addUser $DDA_SYSTEM_USER $DDA_SYSTEM_GROUP
    addUserToGroup $DDA_SYSTEM_USER $DDA_ADMIN_GROUP
    addUserToGroup $DDA_SYSTEM_USER $VIDEO_GROUP
    addUserToGroup $DDA_SYSTEM_USER $GGC_GROUP
fi

validateUser $GGC_USER
addUserToGroup $GGC_USER $DDA_SYSTEM_GROUP

# validate whether users and groups exist on host
validateUser $DDA_SYSTEM_USER
validateUser $DDA_ADMIN_USER
validateGroup $DDA_SYSTEM_GROUP
validateGroup $DDA_ADMIN_GROUP

# # store the id's of the users and groups
rm -rf /tmp/.dda.env
echo "DDA_SYSTEM_USER_ID=$(getUserIdFromName $DDA_SYSTEM_USER)" >> /tmp/.dda.env
echo "DDA_SYSTEM_GROUP_ID=$(getGroupIdFromName $DDA_SYSTEM_GROUP)" >> /tmp/.dda.env
echo "DDA_ADMIN_USER_ID=$(getUserIdFromName $DDA_ADMIN_USER)" >> /tmp/.dda.env
echo "DDA_ADMIN_GROUP_ID=$(getGroupIdFromName $DDA_ADMIN_GROUP)" >> /tmp/.dda.env

# Manage permissions for customer data
if [ -d $DDA_ROOT_DIR ] ; then
    chown $DDA_SYSTEM_USER:$DDA_SYSTEM_GROUP $DDA_ROOT_DIR
    chmod 775 $DDA_ROOT_DIR
fi

# Manage permissions for DDA directories
DDA_GREENGRASS_DIR="${DDA_ROOT_DIR}/greengrass"
for directory in `find ${DDA_ROOT_DIR}/ -maxdepth 1 -mindepth 1 -type d`
do
    # user directories
    if [ $directory != $DDA_GREENGRASS_DIR ] ; then
        chown -R $DDA_ADMIN_USER:$DDA_ADMIN_GROUP $directory
        chmod -R 770 $directory
    fi
done
