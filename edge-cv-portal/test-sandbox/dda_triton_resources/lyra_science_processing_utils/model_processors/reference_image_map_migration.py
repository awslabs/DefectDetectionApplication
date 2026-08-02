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
lines carrying a documented ``# nosem`` justification. This utility MUST be run
by an operator against a *trusted*, first-party map file (one the team produced
during training) -- never against an externally-supplied file, and never on the
inference hot path. The runtime ``__init__`` load path does NOT import this
module and never invokes a code-executing deserializer; it reads only the safe
format.

Usage (offline):
    python -m lyra_science_processing_utils.model_processors.reference_image_map_migration \
        /path/to/legacy_reference_image_map_file

which writes ``<base>.paths.json`` and ``<base>.features.npy`` next to it.
"""
import argparse
import os
import sys

from lyra_science_processing_utils.model_processors.reference_image_map_io import (
    save_safe_reference_image_map,
    derive_safe_paths,
)


def migrate_legacy_map(legacy_map_file: str, reference_image_map_file: str = None):
    """Convert a trusted legacy map to the safe format ONCE.

    :param legacy_map_file: path to the trusted, first-party legacy map.
    :param reference_image_map_file: base path the safe sidecars are derived
        from (defaults to ``legacy_map_file`` so the safe files sit alongside).
    :returns: the ``(paths_json_file, features_npy_file)`` written.
    """
    # Isolated, offline trusted-conversion only. The code-executing deserializer
    # is imported locally so it never enters the inference module's import graph.
    import dill  # nosem: avoid-dill - offline, operator-invoked trusted-conversion only

    if reference_image_map_file is None:
        reference_image_map_file = legacy_map_file

    with open(legacy_map_file, "rb") as handle:
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

    paths_json_file, features_npy_file = migrate_legacy_map(
        args.legacy_map_file, args.reference_image_map_file
    )
    print(f"Wrote safe reference-image map:\n  {paths_json_file}\n  {features_npy_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
