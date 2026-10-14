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
"""Rotated component logs with a total size cap (rtsp-rtmp-stream-cameras
finding 18, Requirement 12.10).

``application.log`` and ``service.log`` rotate every hour and keep 14 days
of hourly files. That bounds their age, not their size: while continuous
stream workflows flooded the log, the MIC-730 held 1.6 GB of
``application.log`` files, and 14 days at that rate is about 15 GB on the
device's persistent storage.

:class:`SizeCappedTimedRotatingFileHandler` keeps the hourly rotation and
the 14-day count, and at each rotation also deletes the oldest rotated
files until they hold at most ``max_rotated_bytes``. The newest rotated
file is always kept, so the most recent hour survives a flood; the disk
use is at most the cap plus the newest rotated file plus the active file.

Standard library only, so it is checked on every device interpreter
(Python 3.10, 3.11) without the backend's dependencies.
"""
import logging.handlers
import os
import re
from typing import List, Optional

MIB = 1024 * 1024

#: The ``strftime`` fields ``TimedRotatingFileHandler`` puts in its rotated
#: file names, as patterns.
_STRFTIME_FIELDS = {"%Y": r"\d{4}", "%m": r"\d{2}", "%d": r"\d{2}",
                    "%H": r"\d{2}", "%M": r"\d{2}", "%S": r"\d{2}"}


def suffix_pattern(suffix: str) -> str:
    """A pattern for the rotated-file suffix ``suffix``, a ``strftime``
    format such as ``%Y-%m-%d_%H``."""
    parts = re.split(r"(%[YmdHMS])", suffix)
    return "".join(_STRFTIME_FIELDS.get(part, re.escape(part)) for part in parts)


class SizeCappedTimedRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    """A ``TimedRotatingFileHandler`` whose rotated files also have a total
    size cap (see the module docstring). ``max_rotated_bytes=None`` keeps
    the plain count limit."""

    def __init__(self, filename, max_rotated_bytes: Optional[int] = None, **kwargs):
        super().__init__(filename, **kwargs)
        self.max_rotated_bytes = None if max_rotated_bytes is None else max(0, int(max_rotated_bytes))

    def rotated_files(self) -> List[str]:
        """This handler's rotated files, oldest first. The suffix format
        sorts chronologically by name."""
        directory, base = os.path.split(self.baseFilename)
        pattern = re.compile(re.escape(base) + r"\." + suffix_pattern(self.suffix))
        try:
            names = os.listdir(directory)
        except OSError:
            return []
        return sorted(os.path.join(directory, name) for name in names if pattern.fullmatch(name))

    def getFilesToDelete(self):
        """The count limit's files, plus the oldest rotated files beyond
        ``max_rotated_bytes``. Called by ``doRollover`` after the active
        file has been rotated."""
        doomed = list(super().getFilesToDelete())
        if self.max_rotated_bytes is None:
            return doomed
        gone = set(doomed)
        kept = [path for path in self.rotated_files() if path not in gone]
        sizes = []
        for path in kept:
            try:
                sizes.append(os.path.getsize(path))
            except OSError:
                sizes.append(0)
        total = sum(sizes)
        # Oldest first, and never the newest rotated file.
        for path, size in zip(kept[:-1], sizes[:-1]):
            if total <= self.max_rotated_bytes:
                break
            doomed.append(path)
            total -= size
        return doomed
