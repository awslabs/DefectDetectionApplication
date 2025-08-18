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

from panorama import panorama_projections
from panorama import trace
from panorama import apidefs

class CredentialProvider(apidefs.BaseProjection):
    def __init__(self, native):
        apidefs.BaseProjection.__init__(self, native)

    def uuid():
        # Must equal ICredentialProvider UUID
        return "8B2F72D1-442F-4A87-8E0D-D7F17396BE4F"

def create_default_aws_credential_provider():
    res = panorama_projections.CreateDefaultAwsCredentialProvider()
    apidefs.CHECKHR(res[0])
    return apidefs.attach(res[1], lambda x: CredentialProvider(x))

def create_from_native_pointer(native):
    return apidefs.assign(native, lambda x: CredentialProvider(x))