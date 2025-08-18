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

while getopts a:s:l:p:r:m:n: flag
do
    case "${flag}" in
        a) aws_access_key_id=${OPTARG};;
        s) aws_secret_access_key=${OPTARG};;
        l) longevity_hrs=${OPTARG};;
        r) aws_region=${OPTARG};;
        m) mqtt_endpoint=${OPTARG};;
        n) payload_size=${OPTARG};;
    esac
done
if tmux has-session -t mysession 2>/dev/null; then
    tmux kill-session -t mysession
fi
tmux new-session -d -s mysession
# Split the terminal and start MockDevice
tmux send-keys -t mysession:0 "aws configure set aws_access_key_id ${aws_access_key_id}; aws configure set aws_secret_access_key ${aws_secret_access_key}; aws configure set region ${aws_region}; MockPanoramaDevice -c /edgemlsdk/longevity.json" C-m

# Wait for a moment to allow mock device to start
sleep 5 
# Split the terminal and run longevity test
# tmux split-window -v -t mysession
# tmux send-keys -t mysession:0.1
export MDS_IP_OVERRIDE=127.0.0.1:8089; export Node_Uid=longevity_node;python3 /edgemlsdk/mqtt/mqtt_longevity.py --trace_level INFO --mqtt_endpoint ${mqtt_endpoint} --longevity_hours ${longevity_hrs}  --region ${aws_region} --payload_size ${payload_size}

# Attach to the tmux session to keep it running
# tmux attach -t mysession