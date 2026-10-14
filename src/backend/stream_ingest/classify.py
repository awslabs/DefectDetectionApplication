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
"""Failure categories of ingest errors (rtsp-rtmp-stream-cameras
Requirements 8.5, 8.6; design "Error Handling").

Pure functions over plain values, so the rules are testable without
GStreamer or PyAV:

- :func:`classify_gst_error` for a GStreamer bus error (domain, code, text);
- :func:`classify_av_error` for a PyAV/FFmpeg failure (errno, text, and
  the FFmpeg log lines captured while it happened).

A failure nothing recognizes is ``network_error``: transient, so an
unexpected message never stops the retries.
"""
import errno as _errno
import re
from typing import Iterable, Optional

from stream_ingest import health

# GLib error domains of GStreamer, and the codes used here (gst/gsterror.h).
GST_RESOURCE_DOMAIN = "gst-resource-error-quark"
GST_STREAM_DOMAIN = "gst-stream-error-quark"
GST_CORE_DOMAIN = "gst-core-error-quark"
RESOURCE_NOT_FOUND = 3
RESOURCE_OPEN_READ = 5
RESOURCE_OPEN_READ_WRITE = 7
RESOURCE_READ = 9
RESOURCE_SETTINGS = 13
RESOURCE_NOT_AUTHORIZED = 15
STREAM_DECODE = 7
STREAM_CODEC_NOT_FOUND = 6
STREAM_TYPE_NOT_FOUND = 4
STREAM_WRONG_TYPE = 5
CORE_MISSING_PLUGIN = 12
CORE_NEGOTIATION = 7

# Status codes only count as whole numbers, so a port such as 5003 or an
# address never reads as a server error.
_RULES = (
    (health.TLS_VERIFICATION_FAILED,
     re.compile(r"certificate|unable to verify|tls handshake|ssl handshake|x509|self[- ]signed")),
    (health.AUTHENTICATION_FAILED,
     re.compile(r"\b40[13]\b|unauthori[sz]ed|not authori[sz]ed|forbidden|authentication"
                r"|authorization failed|bad credentials|invalid credentials|access denied")),
    (health.NOT_FOUND,
     re.compile(r"\b404\b|not found|streamnotfound|no such stream")),
    (health.TIMEOUT,
     re.compile(r"timed out|timeout|could not receive any udp packets|immediate exit requested"
                r"|no data received")),
    (health.SERVER_ERROR,
     re.compile(r"\b50[0-4]\b|internal server error|service unavailable|bad gateway"
                r"|server error")),
)


def _text(*parts: Optional[str]) -> str:
    return " ".join(part for part in parts if isinstance(part, str)).lower()


def classify_text(text: str) -> Optional[str]:
    """The category that message text names, or None. Rules are tried in
    order, so a TLS failure that also says "handshake timed out" is a TLS
    failure."""
    lowered = (text or "").lower()
    for category, pattern in _RULES:
        if pattern.search(lowered):
            return category
    return None


def classify_gst_error(domain: Optional[str], code: Optional[int], message: Optional[str] = None,
                       debug: Optional[str] = None, from_decoder: bool = False,
                       hardware_decoder: bool = False) -> str:
    """The category of a GStreamer bus error.

    ``from_decoder`` says the error came from the decoder element (or the
    decode chain), and ``hardware_decoder`` that it is a hardware decoder:
    such a failure is ``hardware_decoder_failed``, which makes the ``auto``
    policy fall back to software (Requirement 7.5).
    """
    text = _text(message, debug)
    if from_decoder or (domain == GST_STREAM_DOMAIN and code == STREAM_DECODE):
        if hardware_decoder:
            return health.HARDWARE_DECODER_FAILED
        return health.NETWORK_ERROR
    if domain == GST_CORE_DOMAIN and code == CORE_MISSING_PLUGIN:
        return health.DECODER_UNAVAILABLE
    if domain == GST_RESOURCE_DOMAIN:
        if code == RESOURCE_NOT_AUTHORIZED:
            return health.AUTHENTICATION_FAILED
        if code == RESOURCE_NOT_FOUND:
            return health.NOT_FOUND
    if domain == GST_STREAM_DOMAIN and code in (STREAM_CODEC_NOT_FOUND, STREAM_TYPE_NOT_FOUND,
                                                 STREAM_WRONG_TYPE):
        return health.UNSUPPORTED_CODEC
    named = classify_text(text)
    if named is not None:
        return named
    return health.NETWORK_ERROR


def classify_av_error(error_number: Optional[int], message: Optional[str] = None,
                      log_lines: Iterable[str] = ()) -> str:
    """The category of a PyAV/FFmpeg failure while opening or reading an
    RTMP stream. FFmpeg reports RTMP server refusals only in its log
    ("Server error: ..."), so the captured log lines are read too."""
    text = _text(message, *list(log_lines or ()))
    named = classify_text(text)
    if named is not None:
        return named
    if error_number in (_errno.ETIMEDOUT,):
        return health.TIMEOUT
    if error_number in (_errno.ENOENT,):
        return health.NOT_FOUND
    if error_number in (_errno.EACCES, _errno.EPERM):
        return health.AUTHENTICATION_FAILED
    return health.NETWORK_ERROR
