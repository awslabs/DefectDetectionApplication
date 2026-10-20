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

"""The ``run.log`` text-to-entries parser (workflow-debug-mode design §5.1
``recorder.py``/``runlog.py``).

``RunLogCapture`` writes one line per record in ``run_log._LOG_FORMAT``
(``"%(asctime)s %(levelname)s %(name)s: %(message)s"``), where ``asctime`` is
rendered by ``logging.Formatter``'s default converter, **``time.localtime``**,
as ``YYYY-MM-DD HH:MM:SS,mmm``; a multi-line message or an ``exc_info``
traceback follows as continuation lines without a prefix. The parser
therefore reads ``asctime`` as local time with ``time.mktime``: parsing it as
UTC would misorder the merge with the tap's epoch stamps by the host's whole
UTC offset.

Pure and standard-library only; it imports nothing of the engine (its
round-trip test formats a ``LogRecord`` with ``run_log._LOG_FORMAT`` so a
format change in ``run_log.py`` breaks the test).
"""

import re
import time
from typing import List, Tuple

__all__ = ["LINE", "RUNLOG_MAX_ENTRIES", "RUNLOG_MAX_BYTES", "parse"]

LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d{3}) (\w+) ([\w.]+): (.*)$")
RUNLOG_MAX_ENTRIES = 2_000
RUNLOG_MAX_BYTES = 256 * 1024


def parse(text: str, *, fallback_ts_ms: int) -> Tuple[List[dict], int]:
    """``run.log`` text -> ``([{ts, level, logger, message, src: "runlog"}], dropped)``.

    ``asctime`` is local time (``logging.Formatter``'s default converter is
    ``time.localtime``), so it is parsed as local time. A line that matches
    the format but whose date does not parse is a continuation. A first line
    that matches nothing (the cut head of the rolled ``.1`` file) opens an
    ``INFO``/``run.log`` entry at ``fallback_ts_ms``. The result is capped
    at :data:`RUNLOG_MAX_ENTRIES` entries and :data:`RUNLOG_MAX_BYTES` of
    message text, oldest first; ``dropped`` counts the removed entries.
    """
    entries: List[dict] = []
    if not isinstance(text, str):
        return entries, 0
    for line in text.splitlines():
        m = LINE.match(line)
        if m:
            date, ms, level, logger, message = m.groups()
            try:
                ts = int(time.mktime(time.strptime(date, "%Y-%m-%d %H:%M:%S")) * 1000) + int(ms)
            except (ValueError, OverflowError):
                if entries:
                    entries[-1]["message"] += "\n" + line
                else:
                    entries.append({"ts": int(fallback_ts_ms), "level": "INFO",
                                    "logger": "run.log", "message": line, "src": "runlog"})
                continue
            entries.append({"ts": ts, "level": level, "logger": logger,
                            "message": message, "src": "runlog"})
        elif entries:  # continuation: a traceback or a multi-line message
            entries[-1]["message"] += "\n" + line
        else:  # a truncated first line of the rolled .1 file
            entries.append({"ts": int(fallback_ts_ms), "level": "INFO",
                            "logger": "run.log", "message": line, "src": "runlog"})
    # cap: oldest first, counted by the caller in `truncated` as logs.runlog
    dropped = max(0, len(entries) - RUNLOG_MAX_ENTRIES)
    entries = entries[dropped:]
    total = sum(len(e["message"]) for e in entries)
    while entries and total > RUNLOG_MAX_BYTES:
        total -= len(entries[0]["message"])
        entries.pop(0)
        dropped += 1
    return entries, dropped
