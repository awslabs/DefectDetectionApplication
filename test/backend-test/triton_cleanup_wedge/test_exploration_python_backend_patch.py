# Copyright 2026 Amazon Web Services, Inc.
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
"""Exploration (triton-model-cleanup-wedge task 5.1, Property 11): the
python_backend bounded first-message wait is backported onto the Triton
2.45.0 builds, and only there.
The JP5, JP6 and generic (x86, x86 NVIDIA, arm64 CPU) edgemlsdk Dockerfiles
copy ``patches/edgeml-triton-python-backend.diff``, pre-clone python_backend
r24.04 into ``build/python``, apply the patch with an absolute path equal to
the Dockerfile's ``COPY`` destination, check a source sentinel, pass
``--no-force-clone`` to ``build.py`` and check a binary sentinel in the built
``libtriton_python.so``. The patch touches only ``src/stub_launcher.cc`` and
``src/stub_launcher.h``. ``Dockerfile.jp7`` (Triton 2.63.0) does not
reference it.
On the unfixed tree it FAILS: the patch file does not exist and no
Dockerfile references it.
"""

import os
import re
import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
EDGEMLSDK = os.path.join(REPO, "src", "edgemlsdk")
PATCH_NAME = "edgeml-triton-python-backend.diff"
PATCH = os.path.join(EDGEMLSDK, "patches", PATCH_NAME)
PATCHED_DOCKERFILES = ("Dockerfile.jp5", "Dockerfile.jp6", "Dockerfile")
PRE_CLONE = ("git clone --single-branch --depth=1 -b r24.04 "
             "https://github.com/triton-inference-server/python_backend.git build/python")
SOURCE_SENTINEL = 'grep -q "initialization_timeout_ms = 10000" build/python/src/stub_launcher.cc'
BINARY_STRING = "Waiting for the stub health monitoring thread to start"
BINARY_SENTINEL = re.compile(
    r'grep -a -q "' + re.escape(BINARY_STRING) + r'" (\S*)build/python/install/backends/python/libtriton_python\.so')
COPY_RE = re.compile(r"^COPY\s+patches/" + re.escape(PATCH_NAME) + r"\s+(\S+)\s*$", re.M)
APPLY_RE = re.compile(r"git -C build/python apply\s+(\S+)")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _patch_text():
    assert os.path.isfile(PATCH), "the python_backend backport patch %s does not exist" % PATCH
    return _read(PATCH)


def test_patch_touches_only_the_stub_launcher():
    text = _patch_text()
    files = re.findall(r"^diff --git a/(\S+) b/(\S+)$", text, re.M)
    assert files, "the patch has no 'diff --git' headers"
    touched = sorted({a for a, _ in files} | {b for _, b in files})
    assert touched == ["src/stub_launcher.cc", "src/stub_launcher.h"]


def test_patch_carries_the_bounded_wait_and_the_pid_guard():
    text = _patch_text()
    added = [line[1:] for line in text.splitlines()
             if line.startswith("+") and not line.startswith("+++")]
    assert any("initialization_timeout_ms = 10000" in line for line in added)
    # The unbounded Pop() becomes a ReceiveMessageFromStub call with a timeout.
    assert any(re.search(r"ReceiveMessageFromStub\(\s*\w+\s*,\s*\w+", line) for line in added)
    # WaitForStubProcess is guarded so a reaped stub never turns into waitpid(0, ...).
    assert any("if (stub_pid_ != 0)" in line for line in added)
    assert any(BINARY_STRING in line for line in added)


@pytest.mark.parametrize("dockerfile", PATCHED_DOCKERFILES)
def test_dockerfile_copies_and_applies_the_patch_with_its_absolute_copy_path(dockerfile):
    text = _read(os.path.join(EDGEMLSDK, dockerfile))
    copies = COPY_RE.findall(text)
    assert len(copies) == 1, "%s must COPY patches/%s exactly once" % (dockerfile, PATCH_NAME)
    dest = copies[0]
    assert os.path.isabs(dest), "%s COPY destination %r is not absolute" % (dockerfile, dest)
    applies = APPLY_RE.findall(text)
    assert applies, "%s does not run 'git -C build/python apply'" % dockerfile
    for arg in applies:
        # git -C changes directory before it reads its arguments: a relative
        # patch path would resolve inside build/python and fail.
        assert arg == dest, "%s applies %r, expected the COPY destination %r" % (dockerfile, arg, dest)


@pytest.mark.parametrize("dockerfile", PATCHED_DOCKERFILES)
def test_dockerfile_pre_clones_patches_and_checks_both_sentinels(dockerfile):
    text = _read(os.path.join(EDGEMLSDK, dockerfile))
    lines = text.splitlines()
    def first(pred, what):
        for i, line in enumerate(lines):
            if pred(line):
                return i
        pytest.fail("%s: no %s line" % (dockerfile, what))
    server_apply = first(lambda l: "git apply edgeml-triton-server.diff" in l, "server patch apply")
    clone = first(lambda l: PRE_CLONE in l, "python_backend pre-clone")
    apply_ = first(lambda l: APPLY_RE.search(l) is not None, "python_backend patch apply")
    source = first(lambda l: SOURCE_SENTINEL in l, "source sentinel")
    build = first(lambda l: "build.py" in l and "--no-container-build" in l, "build.py")
    no_force = first(lambda l: l.strip().startswith("--no-force-clone"), "--no-force-clone")
    backend = first(lambda l: "--backend python" in l, "--backend python")
    binary = first(lambda l: BINARY_SENTINEL.search(l) is not None, "binary sentinel")
    assert server_apply < clone < apply_ < source < build, \
        "%s: the pre-clone, apply and source check must run before build.py" % dockerfile
    # --no-force-clone is an argument of the same build.py command.
    assert build < no_force < backend
    assert all(lines[i].rstrip().endswith("\\") for i in range(build, no_force + 1))
    assert binary > backend, "%s: the binary sentinel must follow the build" % dockerfile


def test_jp7_does_not_reference_the_backport():
    text = _read(os.path.join(EDGEMLSDK, "Dockerfile.jp7"))
    assert PATCH_NAME not in text
    assert "--no-force-clone" not in text
