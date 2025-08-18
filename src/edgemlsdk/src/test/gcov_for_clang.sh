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

# workaround for executing gcov when compiling with clang
# https://stackoverflow.com/questions/56258782/when-i-compile-my-code-with-clang-gcov-throws-out-of-memory-error

LLVM_COV_PATH=$(ls /usr/bin/llvm-cov*)
exec $LLVM_COV_PATH gcov "$@"
