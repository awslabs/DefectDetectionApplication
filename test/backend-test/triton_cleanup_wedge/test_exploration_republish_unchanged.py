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
"""Exploration, Property 9 (triton-model-cleanup-wedge, task 2.2).

Converting the same model twice (a Startup re-run after a restart) must
leave the published directories untouched the second time: same inodes,
same mtimes. On the unfixed code every conversion deletes and replaces all
three directories, which is a removal under any load in flight
(bugfix.md 1.9).

_Requirements: 1.9, 2.8_
"""
import os

import dda_triton.model_convertor as mc

from triton_cleanup_wedge.fakes import published_dirs, write_fixture_model

MODEL = "model-explore-publish-jetson-xavier-jp5"


def _stats(repo):
    out = {}
    for path in published_dirs(repo, MODEL):
        st = os.stat(path)
        out[os.path.basename(path)] = (st.st_ino, st.st_mtime_ns)
    return out


def test_an_unchanged_republish_leaves_the_published_entries_untouched(tmp_path):
    repo = str(tmp_path / "dda_triton" / "triton_model_repo")
    os.makedirs(repo)
    deployed = write_fixture_model(str(tmp_path / "deployed" / "explore"))

    assert mc.convert_to_triton_structure(repo, deployed, MODEL, "1") is True
    first = _stats(repo)
    assert mc.convert_to_triton_structure(repo, deployed, MODEL, "1") is True
    second = _stats(repo)

    assert second == first, (
        "an unchanged republish replaced published directories: {0} -> {1}".format(
            first, second))
