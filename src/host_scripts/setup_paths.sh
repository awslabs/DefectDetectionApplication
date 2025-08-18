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

# Different locations for aarch vs x86_64
# openssl deb built from source has libraries installed here
arch=$(uname -m)
case "$arch" in
    "x86_64")
        LD_LIBRARY_PATH=${LD_LIBRARY_PATH}:/usr/local/lib64/
        ;;
    "aarch64")
        LD_LIBRARY_PATH=${LD_LIBRARY_PATH}:/usr/local/lib/
        ;;
    *)
        echo "Error: Unsupported architecture '$arch'"
        exit 1
        ;;
esac

# triton server has libraries installed here
LD_LIBRARY_PATH=${LD_LIBRARY_PATH}:/dependencies/server/build/tritonserver/install/lib/stubs/
echo "LD_LIBRARY_PATH=${LD_LIBRARY_PATH}" >> /tmp/.dda.env
