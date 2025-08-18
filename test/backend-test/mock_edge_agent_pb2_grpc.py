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
from unittest.mock import Mock
import sys
import types

module_name = 'edge_agent_pb2_grpc'
edge_agent_pb2_grpc_module = types.ModuleType(module_name)
sys.modules[module_name] = edge_agent_pb2_grpc_module
edge_agent_pb2_grpc_module.require_version = Mock(name=module_name+'.require_version')
sys.modules[module_name+'.repository'] = Mock(name=module_name+'.repository')
