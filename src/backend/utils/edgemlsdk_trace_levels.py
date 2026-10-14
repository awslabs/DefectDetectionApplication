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
"""Which edgemlsdk native INFO traces are per-call chatter
(rtsp-rtmp-stream-cameras finding 18, Requirement 12.6).

edgemlsdk traces a few INFO lines on every inference call or buffer:
``emltriton`` asks Triton for the model's status on every buffer, re-runs
``LoadModel`` on every pipeline start, and traces each result's anomaly
flag and confidence. ``EdgeMLSdkLoggingTraceListener`` forwarded them all
at INFO, so a continuous stream workflow wrote six of them per run into
the component log. Most come from GStreamer streaming threads, where the
``continuous_run`` log context is not set, so the continuous-run filter
cannot drop them.

The listener logs the traces matched here at DEBUG instead, on every
path. They carry nothing the Python side does not already log: the
pipeline runner logs each result itself, and the model readiness checks
log their own waits. Model lifecycle traces (enqueuing, loading, loaded,
already being loaded, unloading) stay at INFO.

Pure and import-safe off-device: the listener itself imports the native
``panorama`` module.
"""
import os
import re
from typing import Pattern, Tuple

#: ``(source file, message pattern)`` of each per-call native INFO trace.
#: The patterns are matched against the whole message, and the test suite
#: checks them against the format strings in the native sources.
PER_CALL_TRACES: Tuple[Tuple[str, Pattern], ...] = (
    # TritonServer::GetModelStatus, called by emltriton on every buffer.
    ("triton_server.cpp", re.compile(r"Model \S+ status is \S+")),
    # TritonServer::LoadModel for a model that is already loaded, called
    # by emltriton on every pipeline start.
    ("triton_server.cpp", re.compile(r"Model \S+ is already loaded")),
    # gst_emltriton_chain, once per inference result.
    ("emltriton.cpp", re.compile(r"Anomalous: .*")),
    ("emltriton.cpp", re.compile(r"Confidence: .*")),
)


def is_per_call_trace(message_file, message) -> bool:
    """Whether a native INFO trace is per-call chatter, logged at DEBUG.

    ``message_file`` is the trace's source file as the native side reports
    it, a base name or a path.
    """
    name = os.path.basename(str(message_file or ""))
    text = str(message or "").strip()
    return any(name == source and pattern.fullmatch(text) for source, pattern in PER_CALL_TRACES)
