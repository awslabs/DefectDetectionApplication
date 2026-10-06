#  #
#   Copyright  Amazon Web Services, Inc.
#  #
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#  #
#        http://www.apache.org/licenses/LICENSE-2.0
#  #
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#  #
"""OFFLINE, one-time migration utility for the reference-image map (finding #5).

This utility converts a *legacy* code-executing reference-image
map into the safe JSON + NumPy format consumed at inference time by
``SupervisedBBoxStage1PostProcessor`` (see ``reference_image_map_io``).

TRUST BOUNDARY / SECURITY NOTE
------------------------------
The legacy loader is a code-executing deserializer and can run arbitrary code
embedded in a crafted file. It is used HERE and ONLY here, behind an explicit,
operator-invoked, OFFLINE trusted-conversion path, on the single import/load
lines carrying a documented ``# nosem`` justification, and only on a file that
``open_trusted_legacy_map`` admits. That check is enforced, not advisory: the
path must resolve strictly below one of ``TRUSTED_LEGACY_MAP_ROOTS`` (component
artifacts, which only the Greengrass nucleus writes, and the Triton model
repository), and the descriptor actually opened must lie below those roots too
and be a regular file owned by root or the effective uid, writable by neither
group nor others. Anything else raises ``UntrustedLegacyMapError`` before the
deserializer is imported, and nothing is loaded or written. The utility is
never on the inference hot path: the runtime ``__init__`` load path does NOT
import this module and never invokes a code-executing deserializer; it reads
only the safe format.

Usage (offline):
    python -m lyra_science_processing_utils.model_processors.reference_image_map_migration \
        /path/to/legacy_reference_image_map_file

which writes ``<base>.paths.json`` and ``<base>.features.npy`` next to it.
"""
import argparse
import os
import stat
import sys

from lyra_science_processing_utils.model_processors.reference_image_map_io import (
    save_safe_reference_image_map,
    derive_safe_paths,
)

# The only directories a legacy map is loaded from (Requirements 10.1, 10.2):
# component artifacts, which only the Greengrass nucleus writes, from verified
# deployments, and the Triton model repository the device converts deployed
# models into (dda_triton/constants.py). Kept here rather than in the backend's
# utils.constants, because this package also ships in the workflow test
# sandbox and the Triton python backend.
TRUSTED_LEGACY_MAP_ROOTS = (
    "/aws_dda/greengrass/v2/packages/artifacts-unarchived",
    "/aws_dda/dda_triton/triton_model_repo",
)


class UntrustedLegacyMapError(ValueError):
    """A legacy map that may not be loaded: outside the trusted roots, not a
    regular file, owned by a uid other than root or the effective uid, or
    group- or world-writable. Nothing has been loaded or written."""


def _refusal(path, condition, actual=None):
    """The :class:`UntrustedLegacyMapError` naming ``path`` (and ``actual``,
    the file it resolves to, when that differs), the failed ``condition``
    and the fix."""
    where = repr(path)
    if actual is not None and actual != path:
        where += " (resolves to {0!r})".format(actual)
    return UntrustedLegacyMapError(
        "refusing to load legacy map {0}: {1}. Nothing was loaded or written. "
        "Move the file under a model artifact root ({2}), or make it "
        "root-owned and not group- or world-writable (chown root, "
        "chmod go-w)".format(where, condition, ", ".join(TRUSTED_LEGACY_MAP_ROOTS))
    )


def _below_trusted_root(resolved):
    """True when the canonical path ``resolved`` lies strictly below one of
    ``TRUSTED_LEGACY_MAP_ROOTS`` as written. The roots are never resolved:
    a symlink put in place of a root or one of its parents would otherwise
    move the trusted location to wherever it points."""
    return any(
        resolved.startswith(root.rstrip("/") + "/")
        for root in TRUSTED_LEGACY_MAP_ROOTS
    )


def _check_trusted_stat(st, path):
    """Raise :class:`UntrustedLegacyMapError` unless ``st``, the ``fstat`` of
    the opened descriptor, shows a regular file owned by uid 0 or the
    effective uid, with neither ``S_IWGRP`` nor ``S_IWOTH`` set."""
    if not stat.S_ISREG(st.st_mode):
        raise _refusal(path, "Not a regular file")
    euid = os.geteuid()
    if st.st_uid not in (0, euid):
        raise _refusal(
            path, "Owned by uid {0}, not root or the effective uid {1}".format(
                st.st_uid, euid))
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise _refusal(path, "Group- or world-writable (mode {0:04o})".format(
            stat.S_IMODE(st.st_mode)))


def open_trusted_legacy_map(path):
    """Open the legacy map at ``path`` for binary reading, only if it is
    trusted (Requirements 10.1, 10.2).

    1. ``os.path.realpath`` resolves ``path``, which must lie strictly below
       one of ``TRUSTED_LEGACY_MAP_ROOTS``; otherwise nothing is opened.
    2. The resolved path is opened with ``O_NOFOLLOW``, so a symlink swapped
       in after step 1 fails with ``ELOOP``, and ``O_NONBLOCK``, so a FIFO is
       refused by step 3 instead of blocking the open (it doesn't affect
       reads of a regular file).
    3. The descriptor actually opened is checked, not a re-resolved path: its
       ``/proc/self/fd`` target must lie below a root as in step 1, and its
       ``fstat`` must pass :func:`_check_trusted_stat`.

    :returns: the file object for the checked descriptor.
    :raises UntrustedLegacyMapError: naming the path, the failed condition
        and the fix; a descriptor already opened is closed first.
    :raises OSError: the file can't be opened (missing, unreadable, or
        ``ELOOP``) or ``/proc/self/fd`` can't be read.
    """
    resolved = os.path.realpath(path)
    if not _below_trusted_root(resolved):
        raise _refusal(path, "Outside the trusted roots", resolved)
    fd = os.open(
        resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        opened = os.readlink("/proc/self/fd/{0}".format(fd))
        if not _below_trusted_root(opened):
            raise _refusal(path, "Outside the trusted roots", opened)
        _check_trusted_stat(os.fstat(fd), path)
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "rb")


def migrate_legacy_map(legacy_map_file: str, reference_image_map_file: str = None):
    """Convert a trusted legacy map to the safe format ONCE.

    :param legacy_map_file: path to the trusted, first-party legacy map; it
        must pass :func:`open_trusted_legacy_map`.
    :param reference_image_map_file: base path the safe sidecars are derived
        from (defaults to ``legacy_map_file`` so the safe files sit alongside).
    :returns: the ``(paths_json_file, features_npy_file)`` written.
    :raises UntrustedLegacyMapError: the file isn't trusted; nothing is loaded
        or written.
    """
    # The trust check runs first, so an untrusted file is refused before the
    # deserializer is even imported.
    trusted_handle = open_trusted_legacy_map(legacy_map_file)
    # Isolated, offline trusted-conversion only. The code-executing deserializer
    # is imported locally so it never enters the inference module's import graph.
    import dill  # nosem: avoid-dill - offline, operator-invoked trusted-conversion only

    if reference_image_map_file is None:
        reference_image_map_file = legacy_map_file

    with trusted_handle as handle:
        # Offline, operator-invoked trusted-conversion of a first-party map;
        # NOT reachable from the inference path.
        data = dill.load(handle)  # nosem: avoid-dill  # noqa: S301
    image_index = data["image_index"]
    return save_safe_reference_image_map(image_index, reference_image_map_file)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Offline one-time migration of a legacy reference-image map "
                    "to the safe JSON + NumPy format."
    )
    parser.add_argument("legacy_map_file", help="Path to the trusted legacy map file.")
    parser.add_argument(
        "--reference-image-map-file", default=None,
        help="Base path the safe sidecars are derived from "
             "(defaults to the legacy file path).",
    )
    args = parser.parse_args(argv)

    if not os.path.exists(args.legacy_map_file):
        parser.error(f"legacy map file not found: {args.legacy_map_file}")

    try:
        paths_json_file, features_npy_file = migrate_legacy_map(
            args.legacy_map_file, args.reference_image_map_file
        )
    except UntrustedLegacyMapError as e:
        parser.error(str(e))
    print(f"Wrote safe reference-image map:\n  {paths_json_file}\n  {features_npy_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
