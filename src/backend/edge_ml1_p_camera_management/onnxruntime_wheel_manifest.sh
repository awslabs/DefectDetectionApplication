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
#
# Reader for onnxruntime-wheels.manifest (beside this file): the per-JetPack
# ORT version, upstream release commit, wheel file name and wheel sha256 that
# build-custom.sh (on the build host, before the image build) and
# install_onnxruntime_gpu.sh (inside the image build) share. Sourceable; no
# side effects on source; bash 3.2 safe; awk + sha256sum/shasum only.
#
#   . onnxruntime_wheel_manifest.sh
#   ort_manifest_version 6            -> "1.20.1"                      (rc 1: no row)
#   ort_manifest_commit 6             -> "5c1b7ccb..."  (40 hex)        (rc 1: no row)
#   ort_manifest_wheel_name 6         -> "onnxruntime_gpu-1.20.1-...whl" (rc 1: no row)
#   ort_manifest_sha256 6             -> the wheel's sha256             (rc 1: no row)
#   ort_manifest_staged_wheel DIR 6   -> "DIR/<wheel>" when that file exists, rc 1 otherwise
#   ort_manifest_verify FILE 6        -> rc 0 when FILE is the row's wheel AND its
#                                        sha256 equals the row's value; rc 2 (and a
#                                        message on stderr) otherwise. Never installs.
#
# The manifest is the only source of these values: a wheel that is not listed,
# or whose digest differs, is never installed; a source build checks out the
# listed commit only.

ORT_MANIFEST_FILE="${ORT_MANIFEST_FILE:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/onnxruntime-wheels.manifest}"
ORT_STAGED_WHEEL_DIRNAME="onnxruntime-wheels"
# The repository the source build clones when no wheel is staged. The release
# commits in the manifest are upstream ids, so any complete mirror serves them.
ORT_SOURCE_REPO_URL="${ORT_SOURCE_REPO_URL:-https://github.com/intel/onnxruntime.git}"

# _ort_manifest_field JETPACK N -> column N of the first data row for JETPACK.
_ort_manifest_field() {
    local jetpack="$1" col="$2"
    [ -n "$jetpack" ] || return 1
    [ -f "$ORT_MANIFEST_FILE" ] || return 1
    awk -v jp="$jetpack" -v col="$col" '
        /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
        $1 == jp { print $col; found = 1; exit }
        END { exit found ? 0 : 1 }' "$ORT_MANIFEST_FILE"
}

ort_manifest_version()    { _ort_manifest_field "$1" 2; }
ort_manifest_wheel_name() { _ort_manifest_field "$1" 3; }
ort_manifest_sha256()     { _ort_manifest_field "$1" 4; }
ort_manifest_commit()     { _ort_manifest_field "$1" 5; }

_ort_file_sha256() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

ort_manifest_verify() {
    local file="$1" jetpack="$2" expected actual
    if [ ! -f "$file" ]; then
        echo "onnxruntime wheel manifest: '$file' is not a file" >&2
        return 2
    fi
    expected="$(ort_manifest_sha256 "$jetpack")" || {
        echo "onnxruntime wheel manifest: no entry for JetPack '$jetpack' in $ORT_MANIFEST_FILE" >&2
        return 2
    }
    case "$(basename -- "$file")" in
        "$(ort_manifest_wheel_name "$jetpack")") ;;
        *) echo "onnxruntime wheel manifest: '$(basename -- "$file")' is not the JetPack $jetpack wheel the manifest names" >&2
           return 2 ;;
    esac
    actual="$(_ort_file_sha256 "$file")"
    if [ "$actual" != "$expected" ]; then
        echo "onnxruntime wheel manifest: sha256 mismatch for $(basename -- "$file") (expected $expected, got $actual); refusing to use it" >&2
        return 2
    fi
    return 0
}

ort_manifest_staged_wheel() {
    local dir="$1" jetpack="$2" name
    name="$(ort_manifest_wheel_name "$jetpack")" || return 1
    [ -f "$dir/$name" ] || return 1
    printf '%s\n' "$dir/$name"
}

# ort_fetch_pinned_source DEST_DIR REPO_URL COMMIT VERSION
#   Clones REPO_URL into DEST_DIR at exactly COMMIT (a 40-hex id fetched by id,
#   so no tag or branch name is resolved on the remote), checks it out detached,
#   initialises the submodules shallowly, and returns 0 only when the tree's
#   VERSION_NUMBER equals VERSION. rc 2 (and a message on stderr) when the
#   commit id is malformed or the version differs; git's own rc when the fetch
#   fails. Leaves the working directory unchanged.
ort_fetch_pinned_source() {
    local dest="$1" repo="$2" commit="$3" version="$4" checked_out
    case "$commit" in
        *[!0-9a-f]*|"") echo "onnxruntime source: '$commit' is not a 40-hex commit id" >&2; return 2 ;;
    esac
    if [ "${#commit}" -ne 40 ]; then
        echo "onnxruntime source: '$commit' is not a 40-hex commit id" >&2
        return 2
    fi
    git init -q "$dest" || return $?
    git -C "$dest" remote add origin "$repo" || return $?
    git -C "$dest" fetch --depth 1 origin "$commit" || return $?
    git -C "$dest" checkout -q --detach FETCH_HEAD || return $?
    git -C "$dest" submodule update --init --recursive --depth 1 || return $?
    checked_out="$(tr -d '[:space:]' < "$dest/VERSION_NUMBER" 2>/dev/null || true)"
    if [ "$checked_out" != "$version" ]; then
        echo "onnxruntime source: commit $commit carries VERSION_NUMBER '$checked_out', not $version; refusing to build it" >&2
        return 2
    fi
    echo "onnxruntime source: checked out $(git -C "$dest" rev-parse HEAD) (VERSION_NUMBER $checked_out)"
    return 0
}
