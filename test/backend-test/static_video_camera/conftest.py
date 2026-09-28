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
"""Session fixtures for the static-video-camera suites.

``clip_library`` synthesizes the clips once per session (see
``video_clip_library``). Suites that need decoding skip with an explicit
reason only when OpenCV or ffmpeg is missing — both ship in every
flask-app image, which is where these suites run."""
import pytest

from video_clip_library import build_library, tools_available


@pytest.fixture(scope="session")
def clip_library(tmp_path_factory):
    reason = tools_available()
    if reason is not None:
        pytest.skip("static-video-camera clip library unavailable: " + reason)
    library = build_library(str(tmp_path_factory.mktemp("video-clips")))
    if not library.decodable():
        pytest.skip("no video clip could be generated: " + "; ".join(library.log))
    return library
