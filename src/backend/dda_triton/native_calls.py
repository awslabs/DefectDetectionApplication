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
"""The lock that serializes Python-side entries into the native Triton
wrapper's shared result buffers (found on hardware, rtsp-rtmp-stream-cameras
task 25.3).

In images built before the fix, edgemlsdk's ``TritonServer::ModelMetadata``
and ``GetModelStatus`` return a pointer into a member string that the next
call on ANY thread replaces. ``emltriton`` initializes on its NULL->READY
state change: it calls ``ModelMetadata`` and parses the text it got back.
When another thread's call replaced that text in between, the parse threw
``nlohmann::json::parse_error`` ("attempting to parse an empty input"),
nothing caught it, and the whole backend process aborted. Two continuous
workflows starting runs together hit it within minutes on JP5.

The native fix (per-thread result buffers, ``triton_server.cpp``) ships with
the edgemlsdk build. This lock keeps every Python-initiated caller apart in
the meantime and on any older image: each pipeline start (the
``set_state(PLAYING)`` call, in which emltriton initializes) and each Python
call of those functions takes it. It is held for milliseconds: model loads
are queued, never awaited, inside it.
"""
import threading

#: Re-entrant, so a start that reaches Python code that asks Triton for a
#: model's status on the same thread cannot deadlock.
TRITON_NATIVE_LOCK = threading.RLock()
