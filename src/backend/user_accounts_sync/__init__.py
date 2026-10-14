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
"""Account_Sync_Service edge agent: applies portal-staged account sets from
the ``dda-user-accounts`` named shadow to the Local_Credential_Cache and
acks through the shadow's reported state.

Public surface for the portal-user-manager feature (Requirements 7.1, 7.4,
7.8, 7.9).
"""
from user_accounts_sync.agent import (
    CACHE_FILE_MODE,
    CACHE_VERSION,
    DEFAULT_CACHE_PATH,
    DOCUMENT_VERSION,
    SHADOW_NAME,
    SyncDocumentError,
    UserAccountsSyncAgent,
    build_cache_document,
    delta_topic_prefix,
    make_shadow_stream_handler,
    parse_sync_document,
    write_cache_atomically,
)

__all__ = [
    "CACHE_FILE_MODE",
    "CACHE_VERSION",
    "DEFAULT_CACHE_PATH",
    "DOCUMENT_VERSION",
    "SHADOW_NAME",
    "SyncDocumentError",
    "UserAccountsSyncAgent",
    "build_cache_document",
    "delta_topic_prefix",
    "make_shadow_stream_handler",
    "parse_sync_document",
    "write_cache_atomically",
]
