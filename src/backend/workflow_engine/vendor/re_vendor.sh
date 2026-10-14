#!/usr/bin/env bash
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
# Re-vendor the shared workflow_core package into the LocalServer
# workflow engine. Run from anywhere; paths are resolved relative to
# this script. See README.md in this directory.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../../.." && pwd)"
SRC="${REPO_ROOT}/edge-cv-portal/backend/layers/workflow_core/python/workflow_core"
DEST="${SCRIPT_DIR}/workflow_core"

if [ ! -d "${SRC}" ]; then
    echo "ERROR: workflow_core source not found at ${SRC}" >&2
    exit 1
fi

echo "Vendoring ${SRC} -> ${DEST}"
rm -rf "${DEST}"
rsync -a --exclude='__pycache__' --exclude='*.pyc' "${SRC}/" "${DEST}/"
echo "Done. Vendored files:"
find "${DEST}" -type f -name '*.py' | sort
