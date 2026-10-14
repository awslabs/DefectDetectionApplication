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
"""Stream_Ingest_Service: RTSP and RTMP stream cameras on the LocalServer
(rtsp-rtmp-stream-cameras, design component 11).

- ``credentials``: the Credential_Store (write-only Stream_Credentials).
- ``manager``: the process-wide ``StreamIngestManager`` of leased sessions.

Each session pulls and decodes its stream in a separate Stream_Worker
process, so a stream failure never takes the backend down.
"""
