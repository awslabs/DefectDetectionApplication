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
"""Property test for the stream camera inventory projection
(rtsp-rtmp-stream-cameras task 18.3).

**Feature: rtsp-rtmp-stream-cameras, Property 13: The stream inventory projection is credential-free and invertible**
**Validates: Requirements 4.5, 5.5**

For any stream Image_Source whose credentials are in the Credential_Store:

- its inventory entry (name, type, params, capabilities, the reported
  document built from it) contains no credential value;
- ``change_to_image_source_data`` applied to the entry's content reproduces
  the Image_Source's URL and stream settings, and the Credential_Reference
  the entry carries is the one stored.
"""
import json
import os

os.environ.setdefault("COMPONENT_WORK_PATH", "/tmp")

from hypothesis import given, settings, strategies as st  # noqa: E402

from camera_sync.agent import build_report_document, change_to_image_source_data  # noqa: E402
from camera_sync.inventory import build_inventory  # noqa: E402
from camera_sync.stream_reporting import stream_change_parts  # noqa: E402
from model import stream_source  # noqa: E402
from workflow_engine.vendor.workflow_core.stream_url import SCHEMES_BY_SOURCE_TYPE  # noqa: E402

hosts = st.sampled_from(["10.0.4.21", "cam-3.plant.local", "192.168.1.64", "[fd00::5]"])
paths = st.sampled_from(["", "/stream1", "/Streaming/Channels/101", "/live/line1", "/cam?profile=main"])


@st.composite
def stream_sources(draw):
    source_type = draw(st.sampled_from(["RTSP", "RTMP"]))
    scheme = draw(st.sampled_from(SCHEMES_BY_SOURCE_TYPE[source_type]))
    port = draw(st.one_of(st.just(""), st.integers(1, 65535).map(lambda value: f":{value}")))
    url = f"{scheme}://{draw(hosts)}{port}{draw(paths)}"
    requested = {}
    if source_type == "RTSP":
        if draw(st.booleans()):
            requested["transport"] = draw(st.sampled_from(stream_source.TRANSPORTS))
        if draw(st.booleans()):
            requested["latencyMs"] = draw(st.integers(*stream_source.LATENCY_MS_RANGE))
    if draw(st.booleans()):
        requested["decoder"] = draw(st.sampled_from(stream_source.DECODER_POLICIES))
    if draw(st.booleans()):
        requested["maxFrameDimension"] = draw(st.integers(*stream_source.MAX_FRAME_DIMENSION_RANGE))
    if draw(st.booleans()):
        requested["stallTimeoutS"] = draw(st.integers(*stream_source.STALL_TIMEOUT_S_RANGE))
    settings_ = stream_source.normalize_stream_settings(source_type, requested)
    if draw(st.booleans()):
        settings_["credentialRef"] = {
            "secretArn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:dda-portal/stream-camera-credentials/dev/cam-AbCdEf",
            "versionId": draw(st.uuids()).hex}
        settings_["credentialsUpdatedAt"] = draw(st.integers(1_700_000_000_000, 1_900_000_000_000))
    marker = draw(st.integers(1000, 9999))
    credentials = {}
    for field in stream_source.CREDENTIAL_FIELDS:
        if draw(st.booleans()):
            credentials[field] = f"{field}-SECRET-{marker}-" + draw(st.text(
                alphabet=st.characters(min_codepoint=33, max_codepoint=126), min_size=0, max_size=12))
    source = {
        "imageSourceId": f"is-{marker}",
        "name": draw(st.sampled_from(["Dock 3", "Line 1 overview", "Gate"])),
        "type": source_type,
        "location": url,
        "imageSourceConfiguration": {"gain": 0, "exposure": 0, "processingPipeline": "",
                                     "streamSettings": settings_},
    }
    return source, credentials


healths = st.one_of(st.none(), st.fixed_dictionaries({
    "state": st.sampled_from(["connecting", "streaming", "reconnecting", "failed", "stopped"]),
    "codec": st.sampled_from(["h264", "h265", None]),
    "width": st.sampled_from([1920, 1280, None]),
    "height": st.sampled_from([1080, 720, None]),
    "decoder": st.sampled_from(["hardware", "software", None]),
    "lastError": st.just({"category": "authentication_failed", "message": "401 Unauthorized", "atMs": 1}),
}))


class TestProperty13:
    @settings(max_examples=25, deadline=None)
    @given(case=stream_sources(), health=healths)
    def test_the_entry_is_credential_free_and_inverts_to_the_source(self, case, health):
        source, credentials = case
        image_source_id = source["imageSourceId"]
        [entry] = build_inventory([source], None, stream_health={image_source_id: health},
                                  stream_credentials_configured={image_source_id: bool(credentials)})

        document = build_report_document([entry], {entry.camera_source_id: 1}, reported_at_ms=1)
        serialized = json.dumps({"entry": entry.__dict__, "document": document}, default=str)
        for value in credentials.values():
            assert value not in serialized, "a credential value reached the inventory"
        assert entry.params["credentialsConfigured"] is bool(credentials)
        assert entry.origin == "edge-configured" and entry.camera_source_id == f"cfg-{image_source_id}"
        assert set(entry.capabilities) == {"stream"}

        change = {"op": "update", "type": entry.type, "name": entry.name, "params": dict(entry.params)}
        data = change_to_image_source_data(change)
        stored = source["imageSourceConfiguration"]["streamSettings"]
        assert data["location"] == source["location"]
        assert data["type"] == source["type"] and data["name"] == source["name"]
        user_settings = {key: value for key, value in stored.items()
                         if key not in stream_source.MANAGED_SETTINGS}
        assert stream_source.normalize_stream_settings(source["type"], data["streamSettings"]) == user_settings
        assert not set(data) & {"credentials", "credentialRef", "credentialsConfigured"}

        _data, reference, clear, updated_at = stream_change_parts(change)
        assert reference == stored.get("credentialRef")
        assert updated_at == stored.get("credentialsUpdatedAt")
        assert clear is (not stored.get("credentialRef") and not credentials)
