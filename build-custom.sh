#!/bin/bash
set -e
set -o pipefail
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

if [ $# -ne 2 ]; then
  echo 1>&2 "Usage: $0 COMPONENT-NAME COMPONENT-VERSION"
  exit 3
fi

COMPONENT_NAME=$1
VERSION=$2
ARCHITECTURE=`uname -m`
# Single source of truth for the DDA interpreter version. Threaded to the
# edgemlsdk build (`-y`) and the docker-compose build args (`PYTHON_VERSION`)
# so the version is set once. Override via the PYTHON_VERSION env var.
PYTHON_VERSION="${PYTHON_VERSION:-3.11}"
export PYTHON_VERSION

# ── Repo-audit guard (spec: python-3-11-security-upgrade) ───────────────────
# This repo has no separate CI pipeline; build-custom.sh IS the build gate, so
# the audit is wired in here to fail fast. test/python_version_audit.py scans
# the scoped build/runtime/provisioning/doc artifacts and exits non-zero
# (printing the offending lines) if a disallowed end-of-life interpreter (the
# unsupported 3.9 series) reference is reintroduced. Running it before the
# (slow) docker builds means a regression aborts the build immediately with a
# clear message. The preserved distro-python references (g-ir-scanner system
# python, host model-conversion python3) are intentionally NOT matched by the
# audit. Set SKIP_PY_AUDIT=1 to bypass (not recommended).
if [ "${SKIP_PY_AUDIT:-0}" = "1" ]; then
  echo "SKIP_PY_AUDIT=1 set — skipping interpreter-version audit guard."
elif command -v python3 >/dev/null 2>&1 && [ -f test/python_version_audit.py ]; then
  echo "Running interpreter-version audit guard (no disallowed 3.9 references)..."
  python3 test/python_version_audit.py \
    || { echo "ERROR: interpreter-version audit failed — a disallowed end-of-life 3.9 interpreter reference reappeared (see hits above). Aborting build."; exit 1; }
  echo "Interpreter-version audit passed (no disallowed 3.9 references)."
else
  echo "WARNING: skipping interpreter-version audit guard (python3 or test/python_version_audit.py not found)."
fi

# change to 20.04 or 18.04
IMAGE_VER="18.04"
#IMAGE_VER="20.04"
BUILDKIT_PROGRESS=plain
export BUILDKIT_PROGRESS
IMAGE_VER=$(grep "DISTRIB_RELEASE" /etc/lsb-release | cut -d'=' -f2)

# Export as environment variable
export IMAGE_VER 

# Determine the JetPack target from the component name.
# JP5 components are named with "JP5" (e.g. aws.edgeml.dda.LocalServer.arm64JP5),
# JP6 components with "JP6" (e.g. aws.edgeml.dda.LocalServer.arm64JP6).
IS_JP5=0
IS_JP6=0
JETPACK_ARG=""
if echo "$COMPONENT_NAME" | grep -q "JP6"; then
    IS_JP6=1
    JETPACK_ARG="6"
elif echo "$COMPONENT_NAME" | grep -q "JP5"; then
    IS_JP5=1
    JETPACK_ARG="5"
fi

echo "Ubuntu version: $IMAGE_VER"
echo "Architecture: $ARCHITECTURE"
echo "JetPack 5: $IS_JP5"
echo "JetPack 6: $IS_JP6"
# copy recipe to greengrass-build
cp recipe.yaml ./greengrass-build/recipes

# create custom build directory
rm -rf ./custom-build
mkdir -p ./custom-build/$COMPONENT_NAME

# build Docker images
# to save build time, remove "--no-cache" parameter
cd src
#edgemlsdk
cd edgemlsdk/
if [ -n "$JETPACK_ARG" ]; then
  ./build.sh -p $(uname -m) -u $IMAGE_VER -y "$PYTHON_VERSION" -j "$JETPACK_ARG"
else
  ./build.sh -p $(uname -m) -u $IMAGE_VER -y "$PYTHON_VERSION"
fi
cd ..
echo "Current directory: $(pwd)"
echo "Checking for edgemlsdk directory: $(ls -ld edgemlsdk 2>&1)"
# Start from a clean staging dir. build-custom.sh is run repeatedly on the same
# build server; a leftover backend/edgemlsdk from a previous run makes the
# `cp -r edgemlsdk backend/edgemlsdk` below merge into a stale nested tree and
# fail (cannot stat .../extracted-debs/debs/*.deb). Removing it first makes every
# run behave like a clean checkout.
rm -rf backend/edgemlsdk
mkdir -p backend/edgemlsdk
if [ ! -d "edgemlsdk" ]; then
  echo "ERROR: edgemlsdk directory not found at $(pwd)/edgemlsdk"
  exit 1
fi
cp -r edgemlsdk backend/edgemlsdk || { echo "ERROR: Failed to copy edgemlsdk from $(pwd)/edgemlsdk to $(pwd)/backend/edgemlsdk"; exit 1; }
echo copying $id
id=$(docker create edgemlsdk)
docker cp $id:/debs/PanoramaSDK.deb $(pwd)/backend/edgemlsdk/
docker cp $id:/debs/aws-c-iot.deb $(pwd)/backend/edgemlsdk/
docker cp $id:/debs/aws-crt-cpp.deb $(pwd)/backend/edgemlsdk/
docker cp $id:/debs/aws-iot-device-sdk-cpp-v2.deb $(pwd)/backend/edgemlsdk/
docker cp $id:/debs/aws-sdk-cpp.deb $(pwd)/backend/edgemlsdk/
docker cp $id:/debs/libgstreamer-plugins-base1.0-dev.deb $(pwd)/backend/edgemlsdk/
docker cp $id:/debs/libgstreamer1.0-dev.deb $(pwd)/backend/edgemlsdk/
docker cp $id:/debs/libgstreamer1.0.deb $(pwd)/backend/edgemlsdk/
docker cp $id:/debs/liborc-0.4-0.deb $(pwd)/backend/edgemlsdk/
docker cp $id:/debs/libstdc++6.deb $(pwd)/backend/edgemlsdk/
docker cp $id:/debs/openssl.deb $(pwd)/backend/edgemlsdk/
docker cp $id:/debs/panorama.whl $(pwd)/backend/edgemlsdk/panorama-1.0-py3-none-any.whl
docker cp $id:/debs/triton-core.deb $(pwd)/backend/edgemlsdk/
docker cp $id:/debs/triton-python-backend.deb $(pwd)/backend/edgemlsdk/
docker cp $id:/tars/triton_installation_files.tar.gz  $(pwd)/backend/edgemlsdk/
docker rm -v $id
echo done copying binaries
# rest of the application
# Select the backend Dockerfile: JP5 uses an L4T r35.x base, JP6 an L4T r36.x base.
if [ "$IS_JP6" = "1" ]; then
  export BACKEND_DOCKERFILE="Dockerfile.jp6"
elif [ "$IS_JP5" = "1" ]; then
  export BACKEND_DOCKERFILE="Dockerfile.jp5"
else
  export BACKEND_DOCKERFILE="Dockerfile"
fi
echo "Backend Dockerfile: $BACKEND_DOCKERFILE"

# ── GPU ONNX Runtime (JP5/JP6) ─────────────────────────────────────────────
# The OnnxRunner uses a GPU (CUDA/TensorRT) onnxruntime built from source in the
# backend image. GPU is enabled by default on JetPack 5 and 6; JetPack 4 stays
# CPU-only (its native python 3.6 has no compatible build path) and x86 uses the
# CPU wheel. The source build is long and memory-heavy (~1-2h), so it can be
# turned off for a fast CPU-only build with ONNXRUNTIME_GPU=0.
if [ "$IS_JP6" = "1" ] || [ "$IS_JP5" = "1" ]; then
  export ONNXRUNTIME_GPU="${ONNXRUNTIME_GPU:-1}"
else
  export ONNXRUNTIME_GPU=0
fi
echo "ONNXRUNTIME_GPU=$ONNXRUNTIME_GPU (1=build GPU onnxruntime from source; JP5/JP6 only. Set 0 for fast CPU-only build)"

echo "Building docker-compose images from $(pwd)/docker-compose.yaml"
# Select profiles by architecture. The `tegra` service targets Jetson
# (platform linux/arm64/v8) and must NOT be built on x86_64 hosts — doing so
# forces an emulated arm64 build that fails compiling Python from source
# ("cannot compute sizeof (long double)"). x86_64 uses only `generic`.
if [ "$ARCHITECTURE" = "x86_64" ]; then
  docker-compose --profile generic -f docker-compose.yaml build --build-arg OS=$IMAGE_VER --build-arg PYTHON_VERSION="$PYTHON_VERSION" --no-cache \
    || { echo "ERROR: docker-compose build failed"; exit 1; }
else
  docker-compose --profile tegra --profile generic -f docker-compose.yaml build \
    --build-arg OS=$IMAGE_VER --build-arg PYTHON_VERSION="$PYTHON_VERSION" \
    --build-arg ONNXRUNTIME_GPU=$ONNXRUNTIME_GPU --no-cache \
    || { echo "ERROR: docker-compose build failed"; exit 1; }
fi
cd ..

# ── Run backend unit tests inside the freshly built flask-app image ─────────
# These tests import the full backend (edgemlsdk bindings, native libs, the
# FastAPI app), so they can only run where those deps already exist — i.e.
# inside the flask-app image we just built. Mount the repo so the tests run
# against the source tree, install the test-only packages on the fly, and run
# pytest. A failure fails the build here, before packaging. Set
# SKIP_BACKEND_TESTS=1 to bypass (e.g. for a quick local rebuild).
if [ "${SKIP_BACKEND_TESTS:-0}" = "1" ]; then
  echo "SKIP_BACKEND_TESTS=1 set — skipping backend unit tests."
else
  echo "Running backend unit tests inside the flask-app image..."
  REPO_ROOT="$(pwd)"
  # PYTHON_VERSION is passed into the container via `-e` so the single-quoted
  # bash -c body stays intact (no fragile quote-breaking in the outer shell);
  # `python${PYTHON_VERSION}` is then expanded by the container's shell at run
  # time, resolving to the live 3.11 interpreter inside the image.
  docker run --rm \
    -v "$REPO_ROOT":/repo -w /repo \
    -e PYTHON_VERSION="$PYTHON_VERSION" \
    --entrypoint bash flask-app -c '
      set -e
      python${PYTHON_VERSION} -m pip install --no-cache-dir --quiet pytest pytest-cov sarge testfixtures hypothesis
      export PYTHONPATH=/repo/src/backend
      # The backend imports the triton/panorama bindings at collection time
      # (via conftest). docker-compose normally provides these loader paths at
      # Run time; replicate them here so libtritonserver.so resolves.
      export LD_LIBRARY_PATH=/opt/tritonserver/lib:/usr/local/cuda/lib64:${LD_LIBRARY_PATH:-}
      python${PYTHON_VERSION} -m pytest \
        test/backend-test/utils/test_auth.py \
        test/backend-test/api-endpoints/test_auth_info_api.py \
        test/backend-test/utils/test_user_group_management_utils.py \
        test/backend-test/utils/test_dda_user_management_utils.py \
        test/backend-test/host_scripts/test_docker_profile_selection.py -v

      # ── Security injection / deserialization gate ─────────────────────────
      # (spec: security-injection-deserialization-fixes). A single green gate:
      #   1. repo_audit.py — pattern gate; exits non-zero if a disallowed
      #      subprocess-interpolation / unsafe-deserializer pattern reappears in
      #      in-scope application code (minus documented # nosem exceptions).
      #   2. Fix-checking suite — every injection/deserialization vector stays
      #      neutralized.
      #   3. Preservation suite — F(X) == F'\''(X) for legitimate inputs
      #      (--noconftest is required: this suite is self-contained and must
      #      not load the backend conftest).
      # set -e above makes any failure here fail the build.
      echo "Running security injection/deserialization audit gate..."
      python${PYTHON_VERSION} test/backend-test/security/repo_audit.py
      python${PYTHON_VERSION} -m pytest \
        test/backend-test/security/test_bug_condition_exploration.py -v
      python${PYTHON_VERSION} -m pytest \
        test/backend-test/security/preservation \
        -p no:cacheprovider --noconftest -v
      echo "Security audit gate passed."

      # ── Security secrets/credentials/JWT audit gate ───────────────────────
      # (spec: security-secrets-credentials-jwt-fixes). A second gate covering
      # the secrets/credentials/JWT batch:
      #   1. secrets_audit.py — pattern gate; exits non-zero if a disallowed
      #      json.dumps(event) log / access_key|secret_key interpolation /
      #      un-annotated verify_signature=False reappears in in-scope source.
      #   2. Fix-checking suite — every secrets/credentials/JWT vector stays
      #      neutralized.
      # Preservation suite is already run by the Group-1 gate above (it covers
      # both specs'\'' baselines).
      echo "Running security secrets/credentials/JWT audit gate..."
      python${PYTHON_VERSION} test/backend-test/security/secrets_audit.py
      python${PYTHON_VERSION} -m pytest \
        test/backend-test/security/test_secrets_bug_condition_exploration.py -v
      echo "Security secrets/credentials/JWT audit gate passed."

      # ── Security IAM / authorization audit gate ───────────────────────────
      # (spec: security-iam-authorization-fixes). A gate covering the IAM
      # least-privilege batch:
      #   1. iam_audit.py — pattern gate; exits non-zero if a disallowed
      #      wildcard-resource-on-scopable-action / service:* action wildcard /
      #      wildcard-account sts:AssumeRole / unenforced-tag PolicyStatement
      #      reappears in in-scope infrastructure code (minus documented
      #      # nosec exceptions).
      #   2. Fix-checking suite — every IAM/authorization vector stays
      #      neutralized (scoped ARNs / tag Conditions / bounded accounts).
      # The security/preservation suite (run by the Group-1 gate above) already
      # covers the IAM preservation baselines (test_preservation_iam_*).
      echo "Running security IAM/authorization audit gate..."
      python${PYTHON_VERSION} test/backend-test/security/iam_audit.py
      python${PYTHON_VERSION} -m pytest \
        test/backend-test/security/test_iam_bug_condition_exploration.py -v
      echo "Security IAM/authorization audit gate passed."

      # ── Security S3 bucket-squatting audit gate ───────────────────────────
      # (spec: security-s3-bucket-squatting-fixes). A gate covering the S3
      # bucket-squatting batch (B1-B6):
      #   1. s3_squat_audit.py — pattern gate; exits non-zero if a predictable
      #      S3 bucket access (aws s3 cp/sync against a hardcoded literal, an
      #      s3:// download URI, or a "bucket" config value) reappears in
      #      in-scope source without an adjacent head-bucket
      #      --expected-bucket-owner preflight, env-var parameterization, or
      #      placeholder / ownership note (per-bucket preflight association).
      #   2. Fix-checking + negative-fixture suite — every S3 access stays
      #      squatting-resistant and the gate cannot be satisfied by a
      #      file-global preflight presence check.
      # The security/preservation suite (run by the Group-1 gate above) already
      # covers the S3 preservation baselines (test_preservation_s3_*).
      echo "Running security S3 bucket-squatting audit gate..."
      python${PYTHON_VERSION} test/backend-test/security/s3_squat_audit.py
      python${PYTHON_VERSION} -m pytest \
        test/backend-test/security/test_s3_squat_bug_condition_exploration.py \
        test/backend-test/security/test_s3_squat_gate_negative_fixture.py -v
      echo "Security S3 bucket-squatting audit gate passed."
    ' || { echo "ERROR: backend unit tests / security audit gate failed"; exit 1; }
  echo "Backend unit tests passed."
fi

# save Docker images as tar
echo "save docker images as tarvballs"
# Use stdout redirection rather than `docker save --output`. Under snap Docker,
# `--output` writes a transient `.tmp-<name><rand>` file in the destination dir
# and renames it; that temp file would briefly appear in the staging dir and
# break the packaging `zip` (exit 18 "could not open for reading"). Redirecting
# stdout lets the shell create the final file directly — no snap temp file.
docker save flask-app > ./custom-build/$COMPONENT_NAME/flask-app.tar
docker save react-webapp > ./custom-build/$COMPONENT_NAME/react-webapp.tar

# include docker-compose.yaml in archive
cp src/docker-compose.yaml ./custom-build/$COMPONENT_NAME/

# include empty directories for each image build context
mkdir -p ./custom-build/$COMPONENT_NAME/backend
mkdir -p ./custom-build/$COMPONENT_NAME/frontend
mkdir -p ./custom-build/$COMPONENT_NAME/host_scripts
mkdir -p ./greengrass-build/artifacts/$COMPONENT_NAME/$VERSION/

# include dio script that triggers output
cp src/backend/triggers/outputs/dio.py ./custom-build/$COMPONENT_NAME/
cp -r src/host_scripts ./custom-build/$COMPONENT_NAME/

# zip up archive
ARCHIVE="./custom-build/$COMPONENT_NAME-$ARCHITECTURE.zip"
rm -f "$ARCHIVE"
# Remove any transient docker-save temp files (e.g. .tmp-react-webapp.tar<rand>)
# that snap Docker may leave briefly in the build dir.
rm -f ./custom-build/$COMPONENT_NAME/.tmp-* 2>/dev/null || true

# Diagnostics: make the next failure conclusive (zip version, what we're about
# to package, and free space on the build volume).
echo "Packaging artifact: $ARCHIVE"
echo "zip version: $(zip --version 2>/dev/null | head -1)"
echo "Staging dir contents:"
ls -lh ./custom-build/$COMPONENT_NAME/ || true
echo "Disk free on build volume:"
df -h ./custom-build/ | tail -n +1 || true

# Package an EXPLICIT member list rather than `zip -r <dir>`. A recursive scan
# of the staging dir can enumerate a transient file (e.g. a snap Docker save
# temp) and then fail with exit 18 / "could not open for reading" /
# "Could not create output file" when that file is renamed away mid-zip.
# Listing only the files we staged removes that race entirely.
ZIP_MEMBERS=(
  "custom-build/$COMPONENT_NAME/docker-compose.yaml"
  "custom-build/$COMPONENT_NAME/flask-app.tar"
  "custom-build/$COMPONENT_NAME/react-webapp.tar"
  "custom-build/$COMPONENT_NAME/dio.py"
  "custom-build/$COMPONENT_NAME/host_scripts"
  "custom-build/$COMPONENT_NAME/backend"
  "custom-build/$COMPONENT_NAME/frontend"
)
zip -r -X "$ARCHIVE" "${ZIP_MEMBERS[@]}" -x '*/.tmp-*' || {
  rc=$?
  echo "ERROR: packaging zip failed (exit $rc)."
  echo "  Staging dir:"
  ls -lh ./custom-build/$COMPONENT_NAME/ || true
  echo "  Disk free:"
  df -h ./custom-build/ || true
  exit $rc
}

# Verify the archive is complete/readable before handing it to GDK. `zip -T`
# is part of zip itself (no unzip dependency required).
if ! zip -T "$ARCHIVE" >/dev/null 2>&1; then
  echo "ERROR: packaged archive $ARCHIVE failed integrity check (zip -T)."
  exit 1
fi
echo "Archive created: $(ls -lh "$ARCHIVE" | awk '{print $5, $9}')"

# copy archive to greengrass-build
cp "$ARCHIVE" ./greengrass-build/artifacts/$COMPONENT_NAME/$VERSION/
