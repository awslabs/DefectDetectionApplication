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
from grpc import StatusCode

class GrpcException(Exception):

    def __init__(self, message, status_code=StatusCode.INTERNAL):
        super().__init__(message)
        self.status_code = self.__convert_grpc_status_to_api_status(status_code)

    def __convert_grpc_status_to_api_status(self, grpc_status):
        if grpc_status == StatusCode.OK:
            return 200
        elif grpc_status ==  StatusCode.INVALID_ARGUMENT:
            return 400
        elif grpc_status ==  StatusCode.FAILED_PRECONDITION:
            return 400
        elif grpc_status ==  StatusCode.NOT_FOUND:
            return 404
        elif grpc_status ==  StatusCode.INTERNAL:
            return 500
        elif grpc_status ==  StatusCode.UNKNOWN:
            return 500
        elif grpc_status ==  StatusCode.RESOURCE_EXHAUSTED:
            return 500
        return 500
