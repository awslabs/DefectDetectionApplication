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
"""Safe on-disk format for the reference-image map used by
``SupervisedBBoxStage1PostProcessor`` (security finding #5, Req 2.5 / 3.5).

The legacy format was a code-executing serialization of
``{'image_index': {path: feature_vector}}``. That deserializer runs arbitrary
code embedded in a crafted file, and the file is *config-driven*
(``config1['reference_image_map_file']``) and may be externally supplied, so it
sits outside any enforceable in-process trust boundary.

This module defines a **safe, non-executable** replacement that reproduces the
exact in-memory result the postprocessor needs:

  * a JSON sidecar (``<base>.paths.json``) holding the ORDERED list of reference
    image paths, and
  * a NumPy ``.npy`` matrix (``<base>.features.npy``) holding the vertically
    stacked feature vectors (``np.vstack`` of the per-path features).

Both are loaded with non-executable loaders (``json.load`` and
``numpy.load(..., allow_pickle=False)``), so a crafted file cannot execute code.
The ordering of ``paths`` and the rows of the feature matrix are kept in lock
step so the reconstructed ``(reference_image_paths, train_feature_gallery)`` is
byte-for-byte identical to what the legacy loader + ``np.vstack`` produced.

NOTE: this module deliberately imports no code-executing deserializer. The
one-time legacy conversion lives in the isolated, offline
``reference_image_map_migration`` utility.
"""
import json
import os
from typing import List, Tuple

import numpy as np

# Suffixes for the two safe-format sidecar files derived from the configured
# ``reference_image_map_file`` base name.
PATHS_SUFFIX = ".paths.json"
FEATURES_SUFFIX = ".features.npy"


def derive_safe_paths(reference_image_map_file: str) -> Tuple[str, str]:
    """Return the ``(paths_json_file, features_npy_file)`` sidecar paths derived
    from the configured ``reference_image_map_file`` base name.

    The base is the map file with any extension stripped, so a legacy
    ``/data/refmap.legacy`` maps to ``/data/refmap.paths.json`` +
    ``/data/refmap.features.npy``.
    """
    base, _ext = os.path.splitext(reference_image_map_file)
    return base + PATHS_SUFFIX, base + FEATURES_SUFFIX


def safe_format_exists(reference_image_map_file: str) -> bool:
    """True iff both safe-format sidecar files exist for the configured map."""
    paths_json_file, features_npy_file = derive_safe_paths(reference_image_map_file)
    return os.path.exists(paths_json_file) and os.path.exists(features_npy_file)


def save_safe_reference_image_map(image_index: dict, reference_image_map_file: str) -> Tuple[str, str]:
    """Write the safe format from a legacy ``image_index`` mapping
    (``{path: feature_vector}``), preserving insertion order.

    Returns the ``(paths_json_file, features_npy_file)`` written. This is used
    only by the offline migration utility / trusted-conversion path -- never at
    inference time.
    """
    paths: List[str] = []
    gallery = []
    for path, feature in image_index.items():
        paths.append(path)
        gallery.append(feature)
    # Exactly the transform the postprocessor applied to the legacy map.
    matrix = np.vstack(gallery)

    paths_json_file, features_npy_file = derive_safe_paths(reference_image_map_file)
    with open(paths_json_file, "w") as handle:
        json.dump(paths, handle)
    # allow_pickle=False: a NumPy matrix of numbers never needs a code-executing
    # deserializer.
    np.save(features_npy_file, matrix, allow_pickle=False)
    return paths_json_file, features_npy_file


def load_safe_reference_image_map(reference_image_map_file: str) -> Tuple[List[str], np.ndarray]:
    """Load the safe format and reconstruct the exact in-memory result the
    postprocessor needs: the ordered ``reference_image_paths`` and the
    ``train_feature_gallery`` (``np.vstack`` matrix).

    Loaded with ``json.load`` + ``numpy.load(..., allow_pickle=False)`` so a
    crafted file cannot execute code.
    """
    paths_json_file, features_npy_file = derive_safe_paths(reference_image_map_file)
    with open(paths_json_file) as handle:
        reference_image_paths = json.load(handle)
    train_feature_gallery = np.load(features_npy_file, allow_pickle=False)
    return reference_image_paths, train_feature_gallery
