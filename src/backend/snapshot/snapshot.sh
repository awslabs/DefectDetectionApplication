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
mkdir -p /aws_dda/system
rm /aws_dda/system/snapshot-*.tar*
#snapshotfile=/snapshot/snapshot-$(date "+%Y.%m.%d-%H.%M.%S").tar
snapshotfile=$(echo $1 | sed -e 's/[^A-Za-z0-9._/-]/_/g')
echo "clean path is "$snapshotfile
touch $snapshotfile

# Add DDA GG component logs and run logs
tar cvf $snapshotfile /aws_dda/greengrass/v2/logs/
tar -rf $snapshotfile /aws_dda/greengrass/v2/work/aws.edgeml.dda.GstRunner/

# GG config
tar -rf $snapshotfile /aws_dda/greengrass/v2/config

# Local Server database files
tar -rf $snapshotfile /aws_dda/greengrass/v2/work/aws.edgeml.dda.LocalServer/

# Memory
cp /proc/meminfo snapshot-meminfo
tar -rf $snapshotfile snapshot-meminfo
rm snapshot-meminfo

# Disk
df > snapshot-df
tar -rf $snapshotfile snapshot-df
rm snapshot-df

# CPU
cp /proc/cpuinfo snapshot-cpuinfo
tar -rf $snapshotfile snapshot-cpuinfo
rm snapshot-cpuinfo

# CPU/memory/IO
vmstat > snapshot-vmstat
tar -rf $snapshotfile snapshot-vmstat
rm snapshot-vmstat

# Processes
ps -ef > snapshot-ps
tar -rf $snapshotfile snapshot-ps
rm snapshot-ps

# compress
gzip $snapshotfile

