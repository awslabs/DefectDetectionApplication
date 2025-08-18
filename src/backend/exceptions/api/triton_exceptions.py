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
from fastapi import HTTPException
from exceptions.api.base_types.validation_exception import ValidationException

class HTTPBaseException(HTTPException):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail

    def as_http_exception(self) -> HTTPException:
        return HTTPException(status_code=self.status_code, detail=self.detail)

class GreengrassOperationException(ValidationException):
    def __init__(self,  message: str, status_code: int = 400):
        super().__init__(status_code=status_code, message=message)

class FileSaveException(HTTPBaseException):
    def __init__(self, detail: str, status_code: int = 400):
        super().__init__(status_code=status_code, detail=detail)

class TritonInternalServerException(HTTPBaseException):
    def __init__(self,   detail: str, status_code: int = 500):
        super().__init__(status_code=status_code, detail=detail)

class InvalidParamterException(HTTPBaseException):
    def __init__(self,   detail: str, status_code: int = 500):
        super().__init__(status_code=status_code, detail=detail)

class UnarchiveFailureException(HTTPBaseException):
    def __init__(self,   detail: str, status_code: int = 500):
        super().__init__(status_code=status_code, detail=detail)

class TritonSetupException(HTTPBaseException):
    def __init__(self,   detail: str, status_code: int = 500):
        super().__init__(status_code=status_code, detail=detail)