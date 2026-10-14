"""Validator unit tests for the stream and scene-analytics rules (task 3.5).

Deterministic example tests for the rules tasks 3.1 and 3.2 added to
``workflow_core/validator/checks.py`` — the generalized V7 frame-feed
coexistence rule plus V11 (Stream_URL), V12 (continuous activation), V13
(analytics configuration) and W3 (no detector upstream). The hypothesis
properties for the same rules live in
``test_property_stream_frame_feed_coexistence.py`` (Property 4) and
``test_property_stream_continuous_activation.py`` (Property 5); this file
pins the two contracts a property test cannot express as well as a literal:

1. **Finding messages name the node, the accepted schemes, and the
   offending parameter** (Requirements 2.1, 2.2). Exact message equality is
   asserted for the headline cases, so the operator-facing text is a pinned
   contract rather than an accident of the shared helper, and every message
   is checked to never echo a credential value.
2. **The existing validator fixtures produce findings identical to their
   current goldens** (Requirement 2.7). ``_FIXTURES`` below is a corpus of
   stream-free, analytics-free graphs taken from the existing validator test
   modules — ``_valid_graph`` and the V1-V5/V7/W1 fixtures of
   ``test_validator_checks.py`` (imported, not copied, so a change there
   travels here), the mixed frame-feed fixture of the custom-python-source
   feature, the V9 subscription-trigger fixtures, and the unified-input
   fixture of ``test_trigger_relocation_and_v7.py``. Each one's **complete**
   findings list — severity, code, message, node id and connection id, in
   emission order — is pinned literally in ``_PRE_FEATURE_GOLDENS``. The
   goldens were captured from the pre-feature validator (HEAD's
   ``checks.py``, run against the same corpus) and verified to be identical
   under the post-feature validator, so a leak of any new rule into a
   stream-free graph, a reworded pre-feature message, or a changed emission
   order fails here with the exact diff.

_Requirements: 2.1, 2.2, 2.7_
"""

from workflow_core.serializer import WorkflowGraph
from workflow_core.validator import (
    CODE_V4_INVALID_PARAMETER_VALUE,
    CODE_V4_MISSING_REQUIRED_PARAMETER,
    CODE_V7_COEXISTENCE_CONFLICT,
    CODE_V11_STREAM_URL,
    CODE_V12_CONTINUOUS_ACTIVATION,
    CODE_V13_ANALYTICS_CONFIG_INVALID,
    CODE_W3_ANALYTICS_NO_DETECTOR,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    validate,
)

# The fixture builders of the existing validator unit tests, imported so
# the golden corpus tracks them instead of duplicating them.
from .test_validator_checks import (
    _aravis,
    _capture,
    _conn,
    _folder,
    _inference,
    _node,
    _rotate,
    _valid_graph,
)

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

#: A Stream_URL that passes every rule, per protocol.
_GOOD_RTSP = "rtsp://192.168.1.64:554/Streaming/Channels/101"
_GOOD_RTMP = "rtmp://media.local/live/line1"

#: Credential material the messages must never echo.
_SECRET = "sup3rs3cret"


def _by_code(findings, code):
    return [f for f in findings if f.code == code]


def _messages(findings):
    return [f.message for f in findings]


def _rtsp(node_id="stream", url=_GOOD_RTSP, **parameters):
    return _node(node_id, "rtsp_camera_source", url=url, **parameters)


def _rtmp(node_id="stream", url=_GOOD_RTMP, **parameters):
    return _node(node_id, "rtmp_stream_source", url=url, **parameters)


def _unified(node_id="stream", source_kind="rtsp_camera", **parameters):
    return _node(node_id, "unified_input", source_kind=source_kind, **parameters)


def _feed_graph(source, extra_nodes=(), extra_connections=()):
    """``source`` -> ``capture``, plus anything the caller adds."""
    return WorkflowGraph(
        nodes=[source, _capture()] + list(extra_nodes),
        connections=[_conn("c1", source.id, "cap")] + list(extra_connections),
    )


def _analytics_graph(node):
    """``folder_source`` -> ``model_inference`` -> ``node`` -> ``capture``.

    A detector is upstream, so W3 stays silent and V13 is the only rule the
    analytics node can trip.
    """
    return WorkflowGraph(
        nodes=[_folder(), _inference(), node, _capture()],
        connections=[
            _conn("c1", "src", "inf"),
            _conn("c2", "inf", node.id),
            _conn("c3", node.id, "cap"),
        ],
    )


# --------------------------------------------------------------------------
# V11: the message names the node, the parameter, and the accepted schemes
# (Requirement 2.1)
# --------------------------------------------------------------------------

class TestV11AcceptedSchemes:
    def test_rtsp_node_with_rtmp_url_names_node_parameter_and_schemes(self):
        found = _by_code(validate(_feed_graph(_rtsp(url=_GOOD_RTMP))),
                         CODE_V11_STREAM_URL)
        assert len(found) == 1
        assert found[0].severity == SEVERITY_ERROR
        assert found[0].node_id == "stream"
        assert found[0].message == (
            "Node 'stream': parameter 'url': Stream URL scheme 'rtmp' is not "
            "allowed here; accepted schemes are rtsp, rtsps."
        )

    def test_rtmp_node_with_rtsp_url_names_the_rtmp_schemes(self):
        found = _by_code(validate(_feed_graph(_rtmp(url=_GOOD_RTSP))),
                         CODE_V11_STREAM_URL)
        assert len(found) == 1
        assert found[0].message == (
            "Node 'stream': parameter 'url': Stream URL scheme 'rtsp' is not "
            "allowed here; accepted schemes are rtmp, rtmps."
        )

    def test_foreign_scheme_names_both_accepted_schemes_of_the_type(self):
        for source, expected in (
            (_rtsp(url="http://cam.local/stream"), "rtsp, rtsps"),
            (_rtmp(url="http://cam.local/stream"), "rtmp, rtmps"),
        ):
            found = _by_code(validate(_feed_graph(source)), CODE_V11_STREAM_URL)
            assert len(found) == 1, source.type
            assert "'http'" in found[0].message
            assert expected in found[0].message

    def test_each_type_accepts_its_own_secure_scheme(self):
        for source in (_rtsp(url="rtsps://cam.local:322/stream"),
                       _rtmp(url="rtmps://media.local:443/live/line1")):
            assert _by_code(validate(_feed_graph(source)),
                            CODE_V11_STREAM_URL) == [], source.type

    def test_valid_stream_url_draws_no_v11_finding(self):
        for source in (_rtsp(), _rtmp()):
            assert _by_code(validate(_feed_graph(source)),
                            CODE_V11_STREAM_URL) == [], source.type

    def test_host_less_url_names_the_node_and_the_parameter(self):
        found = _by_code(validate(_feed_graph(_rtsp(url="rtsp:///stream"))),
                         CODE_V11_STREAM_URL)
        assert len(found) == 1
        assert found[0].node_id == "stream"
        assert found[0].message.startswith("Node 'stream': parameter 'url': ")
        assert "must contain a host" in found[0].message

    def test_missing_url_is_reported_by_v11_as_well_as_v4(self):
        """``url`` is required, so V4 also reports it; the codes are
        distinct and V11 carries the form the operator needs."""
        findings = validate(_feed_graph(_node("stream", "rtsp_camera_source")))
        v11 = _by_code(findings, CODE_V11_STREAM_URL)
        assert len(v11) == 1
        assert v11[0].message == (
            "Node 'stream': parameter 'url': Stream URL is required and must "
            "be a non-empty string of the form rtsp://host[:port][/path]."
        )
        assert [f.node_id for f in
                _by_code(findings, CODE_V4_MISSING_REQUIRED_PARAMETER)] == ["stream"]

    def test_non_lowercase_scheme_is_reported_with_the_lowercase_spelling(self):
        found = _by_code(validate(_feed_graph(_rtsp(url="RTSP://cam.local/s"))),
                         CODE_V11_STREAM_URL)
        assert len(found) == 1
        assert found[0].message == (
            "Node 'stream': parameter 'url': Stream URL scheme must be "
            "lowercase; write 'rtsp' instead of 'RTSP'."
        )

    def test_one_finding_per_node_naming_each_node(self):
        """Two bad stream nodes get one V11 finding each, each naming its
        own node id (the coexistence conflict they also draw is V7's)."""
        graph = WorkflowGraph(
            nodes=[_rtsp("a", url="http://a.local/s"),
                   _rtmp("b", url="http://b.local/s"),
                   _capture()],
            connections=[_conn("c1", "a", "cap"), _conn("c2", "b", "cap")],
        )
        found = _by_code(validate(graph), CODE_V11_STREAM_URL)
        assert [f.node_id for f in found] == ["a", "b"]
        assert found[0].message.startswith("Node 'a': parameter 'url': ")
        assert found[1].message.startswith("Node 'b': parameter 'url': ")

    def test_only_one_finding_even_when_several_rules_are_broken(self):
        """A URL with user information *and* a secret query parameter still
        yields exactly one V11 finding."""
        found = _by_code(
            validate(_feed_graph(_rtsp(
                url="rtsp://admin:{0}@cam.local/s?token={0}".format(_SECRET)))),
            CODE_V11_STREAM_URL,
        )
        assert len(found) == 1


class TestV11OnUnifiedNodes:
    """A unified node is evaluated through its effective ``source_kind``
    (design component 3), so save-time validation of the unexpanded graph
    agrees with compile-time validation of the expanded one."""

    def test_unified_stream_kind_is_checked_with_its_type_s_schemes(self):
        found = _by_code(
            validate(_feed_graph(_unified(source_kind="rtsp_camera",
                                          url=_GOOD_RTMP))),
            CODE_V11_STREAM_URL,
        )
        assert len(found) == 1
        assert found[0].node_id == "stream"
        assert "accepted schemes are rtsp, rtsps." in found[0].message

    def test_unified_rtmp_kind_accepts_an_rtmp_url(self):
        assert _by_code(
            validate(_feed_graph(_unified(source_kind="rtmp_stream",
                                          url=_GOOD_RTMP))),
            CODE_V11_STREAM_URL,
        ) == []

    def test_unified_non_stream_kind_is_never_checked(self):
        """A pre-feature unified node — even one carrying a ``url`` value —
        draws no V11 finding, because its effective type is not a stream
        type (Requirement 2.7)."""
        for kind in ("folder", "csi_camera", "icam", "aravis_camera"):
            graph = _feed_graph(_unified(source_kind=kind,
                                         location="/data/images",
                                         url="http://not-a-stream/x"))
            assert _by_code(validate(graph), CODE_V11_STREAM_URL) == [], kind


class TestV11CredentialsBelongInTheCamera:
    """Requirement 2.2: embedded user information and Secret_Query_Parameters
    are errors whose message says credentials belong in the camera's
    configuration — and never echoes the credential."""

    def test_user_information_message_names_the_node_and_the_remedy(self):
        url = "rtsp://admin:{0}@cam.local/Streaming/Channels/101".format(_SECRET)
        found = _by_code(validate(_feed_graph(_rtsp(url=url))),
                         CODE_V11_STREAM_URL)
        assert len(found) == 1
        assert found[0].message == (
            "Node 'stream': parameter 'url': Stream URL must not contain "
            "embedded user information (the 'user:password@' part before the "
            "host); credentials belong in the camera's configuration, not in "
            "the URL."
        )

    def test_secret_query_parameter_message_names_the_parameter(self):
        found = _by_code(
            validate(_feed_graph(_rtmp(
                url="rtmp://media.local/live/line1?streamkey={0}".format(_SECRET)))),
            CODE_V11_STREAM_URL,
        )
        assert len(found) == 1
        assert found[0].message == (
            "Node 'stream': parameter 'url': Stream URL query parameter "
            "'streamkey' carries credentials; credentials belong in the "
            "camera's configuration, not in the URL."
        )

    def test_secret_query_parameter_is_named_as_written(self):
        """Matching is case-insensitive; the message quotes the operator's
        own spelling so the parameter is findable in the URL."""
        for written in ("Token", "API_KEY", "pwd", "Signature"):
            url = "rtsp://cam.local/s?{0}={1}".format(written, _SECRET)
            found = _by_code(validate(_feed_graph(_rtsp(url=url))),
                             CODE_V11_STREAM_URL)
            assert len(found) == 1, written
            assert "'{0}'".format(written) in found[0].message
            assert "credentials belong in the camera's configuration" in \
                found[0].message

    def test_no_finding_message_ever_echoes_the_credential(self):
        """Across every finding of every rule — V11's included — a graph
        whose URL carries credentials leaks neither the password nor the
        secret query value."""
        urls = (
            "rtsp://admin:{0}@cam.local/s".format(_SECRET),
            "rtsp://cam.local/s?password={0}".format(_SECRET),
            "rtsp://cam.local/s?token={0}&pwd={0}".format(_SECRET),
        )
        for url in urls:
            findings = validate(_feed_graph(_rtsp(url=url)))
            assert findings, url
            for message in _messages(findings):
                assert _SECRET not in message, (url, message)

    def test_a_non_secret_query_parameter_is_accepted(self):
        graph = _feed_graph(_rtsp(url="rtsp://cam.local/s?channel=1&subtype=0"))
        assert _by_code(validate(graph), CODE_V11_STREAM_URL) == []


# --------------------------------------------------------------------------
# V7: the generalized frame-feed coexistence message (Requirement 2.4)
# --------------------------------------------------------------------------

class TestV7StreamCoexistenceMessages:
    def test_two_stream_nodes_of_one_type_name_the_type_and_both_members(self):
        graph = WorkflowGraph(nodes=[_rtsp("s1"), _rtsp("s2"), _capture()])
        found = _by_code(validate(graph), CODE_V7_COEXISTENCE_CONFLICT)
        assert [f.node_id for f in found] == ["s1", "s2"]
        assert all(f.severity == SEVERITY_ERROR for f in found)
        assert _messages(found) == [
            "Node 's1': 2 nodes of type 'rtsp_camera_source' cannot coexist "
            "in one workflow ('s1', 's2'): the single-frame appsrc feed "
            "serves exactly one frame-feed source per workflow",
            "Node 's2': 2 nodes of type 'rtsp_camera_source' cannot coexist "
            "in one workflow ('s1', 's2'): the single-frame appsrc feed "
            "serves exactly one frame-feed source per workflow",
        ]

    def test_two_rtmp_nodes_name_their_own_type(self):
        graph = WorkflowGraph(nodes=[_rtmp("s1"), _rtmp("s2"), _capture()])
        found = _by_code(validate(graph), CODE_V7_COEXISTENCE_CONFLICT)
        assert [f.node_id for f in found] == ["s1", "s2"]
        for finding in found:
            assert "2 nodes of type 'rtmp_stream_source'" in finding.message

    def test_mixed_frame_feed_types_name_every_member(self):
        graph = WorkflowGraph(
            nodes=[_rtsp("s1"), _rtmp("s2"), _aravis("a1"), _capture()])
        found = _by_code(validate(graph), CODE_V7_COEXISTENCE_CONFLICT)
        assert [f.node_id for f in found] == ["a1", "s1", "s2"]
        for finding in found:
            assert "frame-feed source nodes ('a1', 's1', 's2')" in finding.message

    def test_a_single_stream_node_draws_no_coexistence_finding(self):
        for source in (_rtsp(), _rtmp()):
            assert _by_code(validate(_feed_graph(source)),
                            CODE_V7_COEXISTENCE_CONFLICT) == [], source.type


# --------------------------------------------------------------------------
# V12: the continuous-activation message (Requirements 2.3, 2.5)
# --------------------------------------------------------------------------

class TestV12Messages:
    def test_activation_edge_message_names_node_port_and_remedy(self):
        graph = _feed_graph(
            _rtsp(),
            extra_nodes=[_node("din", "digital_input", pin=1)],
            extra_connections=[_conn("t1", "din", "stream",
                                     target_port="activation")],
        )
        found = _by_code(validate(graph), CODE_V12_CONTINUOUS_ACTIVATION)
        assert len(found) == 1
        assert found[0].severity == SEVERITY_ERROR
        assert found[0].node_id == "stream"
        assert found[0].message == (
            "Node 'stream': continuous processing is its own activation "
            "model, but the workflow has a connection into its 'activation' "
            "port. Set 'processing_mode' to 'on_trigger' to drive this node "
            "from a trigger, or remove the trigger"
        )

    def test_subscription_trigger_message_names_the_trigger(self):
        graph = _feed_graph(
            _rtsp(),
            extra_nodes=[_node("msub", "mqtt_subscribe",
                               topic="factory/line1/trigger", greengrass=True)],
        )
        found = _by_code(validate(graph), CODE_V12_CONTINUOUS_ACTIVATION)
        assert len(found) == 1
        assert "'msub'" in found[0].message
        assert "subscription trigger node(s)" in found[0].message

    def test_a_continuous_node_is_reported_once_not_twice(self):
        """V9 skips continuous stream nodes, so a graph mixing a
        subscription trigger with one gets a single finding on that node —
        V12's (Requirement 2.3)."""
        graph = _feed_graph(
            _rtsp(),
            extra_nodes=[_node("msub", "mqtt_subscribe",
                               topic="factory/line1/trigger", greengrass=True)],
        )
        on_stream = [f for f in validate(graph) if f.node_id == "stream"]
        assert [f.code for f in on_stream] == [CODE_V12_CONTINUOUS_ACTIVATION]

    def test_on_trigger_node_draws_no_v12_finding(self):
        graph = _feed_graph(
            _rtsp(processing_mode="on_trigger"),
            extra_nodes=[_node("msub", "mqtt_subscribe",
                               topic="factory/line1/trigger", greengrass=True)],
            extra_connections=[_conn("t1", "msub", "stream",
                                     target_port="activation")],
        )
        assert _by_code(validate(graph), CODE_V12_CONTINUOUS_ACTIVATION) == []

    def test_a_lone_continuous_stream_node_draws_no_v12_finding(self):
        assert _by_code(validate(_feed_graph(_rtsp())),
                        CODE_V12_CONTINUOUS_ACTIVATION) == []


# --------------------------------------------------------------------------
# V13: the analytics-configuration message names the offending parameter
# (Requirements 13.7, 14.6)
# --------------------------------------------------------------------------

class TestV13Messages:
    def test_malformed_zone_names_the_node_and_the_parameter(self):
        node = _node("count", "detection_counter", classes="person",
                     zone="not json")
        found = _by_code(validate(_analytics_graph(node)),
                         CODE_V13_ANALYTICS_CONFIG_INVALID)
        assert len(found) == 1
        assert found[0].severity == SEVERITY_ERROR
        assert found[0].node_id == "count"
        assert found[0].message == (
            "Node 'count': parameter 'zone': Zone is not valid JSON."
        )

    def test_malformed_class_list_names_the_classes_parameter(self):
        node = _node("count", "detection_counter", classes="person, , hardhat")
        found = _by_code(validate(_analytics_graph(node)),
                         CODE_V13_ANALYTICS_CONFIG_INVALID)
        assert len(found) == 1
        assert found[0].message == (
            "Node 'count': parameter 'classes': Label list entry 2 is empty; "
            "remove the extra comma."
        )

    def test_one_finding_per_offending_parameter(self):
        """Two malformed parameters on one node give two findings, each
        naming its own parameter; several problems in one parameter stay
        one finding."""
        node = _node("assoc", "object_association", subject_class="person, dog",
                     required_classes="hardhat", zone="[[2, 0], [0, 3]]")
        found = _by_code(validate(_analytics_graph(node)),
                         CODE_V13_ANALYTICS_CONFIG_INVALID)
        assert [f.node_id for f in found] == ["assoc", "assoc"]
        parameters = [m.split("parameter '")[1].split("'")[0] for m in
                      _messages(found)]
        assert parameters == ["subject_class", "zone"]
        # The zone reports three problems (two coordinates, one point count)
        # in one finding.
        zone_message = _messages(found)[1]
        assert zone_message.count("Zone ") == 3

    def test_several_label_problems_stay_one_finding(self):
        """A label list with two problems — an empty entry and an entry with
        no addressable characters — is one finding carrying both."""
        node = _node("count", "detection_counter", classes="person, , ***")
        found = _by_code(validate(_analytics_graph(node)),
                         CODE_V13_ANALYTICS_CONFIG_INVALID)
        assert len(found) == 1
        assert found[0].message == (
            "Node 'count': parameter 'classes': Label list entry 2 is empty; "
            "remove the extra comma. Label '***' has no letters or digits, so "
            "it cannot be used as a metadata key."
        )

    def test_well_formed_analytics_configuration_draws_nothing(self):
        node = _node("count", "detection_counter", classes="person, hardhat",
                     zone="[[0, 0], [1, 0], [1, 1]]", zone_rule="center")
        assert _by_code(validate(_analytics_graph(node)),
                        CODE_V13_ANALYTICS_CONFIG_INVALID) == []

    def test_blank_values_are_v4_s_business_not_v13_s(self):
        node = _node("assoc", "object_association", subject_class="",
                     required_classes="", zone="")
        findings = validate(_analytics_graph(node))
        assert _by_code(findings, CODE_V13_ANALYTICS_CONFIG_INVALID) == []
        assert [f.node_id for f in
                _by_code(findings, CODE_V4_INVALID_PARAMETER_VALUE)] == \
            ["assoc", "assoc"]

    def test_event_gate_is_never_reported_by_v13(self):
        node = _node("gate", "event_gate", condition="counter.people.total > 5")
        assert _by_code(validate(_analytics_graph(node)),
                        CODE_V13_ANALYTICS_CONFIG_INVALID) == []


# --------------------------------------------------------------------------
# W3: the no-detector warning (Requirement 13.8)
# --------------------------------------------------------------------------

class TestW3Messages:
    def _graph_without_detector(self, node):
        return WorkflowGraph(
            nodes=[_folder(), node, _capture()],
            connections=[_conn("c1", "src", node.id), _conn("c2", node.id, "cap")],
        )

    def test_warning_names_the_node_and_the_missing_detector_type(self):
        node = _node("count", "detection_counter", classes="person")
        found = _by_code(validate(self._graph_without_detector(node)),
                         CODE_W3_ANALYTICS_NO_DETECTOR)
        assert len(found) == 1
        assert found[0].severity == SEVERITY_WARNING
        assert found[0].node_id == "count"
        assert found[0].message == (
            "Node 'count': no 'model_inference' node is upstream of this "
            "'detection_counter' node, so no detections will exist for it to "
            "analyze at runtime"
        )

    def test_object_association_warns_too_naming_its_own_type(self):
        node = _node("assoc", "object_association", subject_class="person",
                     required_classes="hardhat")
        found = _by_code(validate(self._graph_without_detector(node)),
                         CODE_W3_ANALYTICS_NO_DETECTOR)
        assert len(found) == 1
        assert "'object_association' node" in found[0].message

    def test_a_detector_directly_upstream_silences_the_warning(self):
        node = _node("count", "detection_counter", classes="person")
        assert _by_code(validate(_analytics_graph(node)),
                        CODE_W3_ANALYTICS_NO_DETECTOR) == []

    def test_a_transitively_upstream_detector_silences_the_warning(self):
        """model_inference -> rotate -> detection_counter: upstream means
        transitively upstream, not directly connected."""
        node = _node("count", "detection_counter", classes="person")
        graph = WorkflowGraph(
            nodes=[_folder(), _inference(), _rotate(), node, _capture()],
            connections=[
                _conn("c1", "src", "inf"),
                _conn("c2", "inf", "rot"),
                _conn("c3", "rot", "count"),
                _conn("c4", "count", "cap"),
            ],
        )
        assert _by_code(validate(graph), CODE_W3_ANALYTICS_NO_DETECTOR) == []

    def test_a_detector_downstream_does_not_count(self):
        node = _node("count", "detection_counter", classes="person")
        graph = WorkflowGraph(
            nodes=[_folder(), node, _inference(), _capture()],
            connections=[
                _conn("c1", "src", "count"),
                _conn("c2", "count", "inf"),
                _conn("c3", "inf", "cap"),
            ],
        )
        found = _by_code(validate(graph), CODE_W3_ANALYTICS_NO_DETECTOR)
        assert [f.node_id for f in found] == ["count"]

    def test_event_gate_is_never_warned_about(self):
        node = _node("gate", "event_gate", condition="counter.people.total > 5")
        assert _by_code(validate(self._graph_without_detector(node)),
                        CODE_W3_ANALYTICS_NO_DETECTOR) == []


# --------------------------------------------------------------------------
# Requirement 2.7: the existing validator fixtures produce findings
# identical to their current goldens
# --------------------------------------------------------------------------

def _custom_python_source(node_id="py"):
    return _node(node_id, "custom_python_source",
                 code="def produce_frame(context):\n    return None\n")


def _mqtt_subscribe(node_id="msub"):
    return _node(node_id, "mqtt_subscribe", topic="factory/line1/trigger",
                 greengrass=True)


def _fixtures():
    """Stream-free, analytics-free fixture graphs from the existing
    validator test modules, keyed by name.

    Built fresh on every call (``WorkflowGraph`` holds mutable lists) so a
    test can never observe another test's mutation.
    """
    return {
        # test_validator_checks.TestValidGraph
        "valid_folder_capture": _valid_graph(),
        # test_validator_checks.TestV1
        "empty_graph": WorkflowGraph(),
        "no_input_node": WorkflowGraph(nodes=[_capture()]),
        "no_output_node": WorkflowGraph(nodes=[_folder()]),
        # test_validator_checks.TestV2
        "incompatible_port_types": WorkflowGraph(
            nodes=[_node("din", "digital_input", pin=1), _rotate(), _capture()],
            connections=[_conn("bad", "din", "rot")],
        ),
        "coerced_inference_to_capture": WorkflowGraph(
            nodes=[_folder(), _inference(), _capture()],
            connections=[_conn("c1", "src", "inf"), _conn("c2", "inf", "cap")],
        ),
        # test_validator_checks.TestV3
        "two_node_cycle": WorkflowGraph(
            nodes=[_folder(), _rotate("rotA"), _rotate("rotB"), _capture()],
            connections=[
                _conn("c1", "src", "rotA"),
                _conn("c2", "rotA", "rotB"),
                _conn("c3", "rotB", "rotA"),
                _conn("c4", "rotA", "cap"),
            ],
        ),
        # test_validator_checks.TestV4
        "missing_required_parameter": WorkflowGraph(
            nodes=[_node("src2", "folder_source"), _capture()],
            connections=[_conn("c1", "src2", "cap")],
        ),
        "invalid_parameter_value": WorkflowGraph(
            nodes=[_folder(), _node("cap", "capture", output_path="/out",
                                    quality=200)],
            connections=[_conn("c1", "src", "cap")],
        ),
        # test_validator_checks.TestV5
        "detached_node": WorkflowGraph(
            nodes=[_folder(), _capture(), _rotate("stray")],
            connections=[_conn("c1", "src", "cap")],
        ),
        # test_validator_checks.TestV7
        "two_aravis_sources": WorkflowGraph(
            nodes=[_aravis("a1"), _aravis("a2"), _capture()]),
        "single_aravis_source": WorkflowGraph(
            nodes=[_aravis("a1"), _capture()],
            connections=[_conn("c1", "a1", "cap")],
        ),
        # custom-python-source Requirement 8.2: the mixed frame-feed rule.
        "aravis_and_custom_python_source": WorkflowGraph(
            nodes=[_aravis("a1"), _custom_python_source(), _capture()],
            connections=[_conn("c1", "a1", "cap"), _conn("c2", "py", "cap")],
        ),
        "two_custom_python_sources": WorkflowGraph(
            nodes=[_custom_python_source("py1"), _custom_python_source("py2"),
                   _capture()],
        ),
        # trigger-activation-runtime Requirement 4.1: V9.
        "subscription_trigger_activation_unconnected": WorkflowGraph(
            nodes=[_mqtt_subscribe(), _folder(), _capture()],
            connections=[_conn("c1", "src", "cap")],
        ),
        "subscription_trigger_activation_connected": WorkflowGraph(
            nodes=[_mqtt_subscribe(), _folder(), _capture()],
            connections=[
                _conn("t1", "msub", "src", target_port="activation"),
                _conn("c1", "src", "cap"),
            ],
        ),
        # test_trigger_relocation_and_v7.TestV7StageOrder
        "unified_folder_input_with_trigger": WorkflowGraph(
            nodes=[_node("din", "digital_input", pin=1),
                   _node("uni", "unified_input", source_kind="folder",
                         location="/data/images"),
                   _capture()],
            connections=[
                _conn("t1", "din", "uni", target_port="activation"),
                _conn("c1", "uni", "cap"),
            ],
        ),
        "connection_targets_a_trigger": WorkflowGraph(
            nodes=[_folder(), _node("din", "digital_input", pin=1), _capture()],
            connections=[_conn("bad", "src", "din")],
        ),
        # test_validator_checks.TestCompleteness
        "unknown_node_type": WorkflowGraph(
            nodes=[_node("mystery", "not_a_real_type"), _folder(), _capture()],
            connections=[_conn("c1", "src", "cap"),
                         _conn("c2", "mystery", "cap")],
        ),
        "many_defect_classes": WorkflowGraph(
            nodes=[
                _rotate("rotA"), _rotate("rotB"),
                _node("filt", "inference_filter"),
                _node("stray", "rotate", method="clockwise"),
                _capture(),
            ],
            connections=[
                _conn("c1", "rotA", "rotB"),
                _conn("c2", "rotB", "rotA"),
                _conn("c3", "rotA", "filt"),
            ],
        ),
    }


def _finding_tuples(findings):
    return [
        (f.severity, f.code, f.message, f.node_id, f.connection_id)
        for f in findings
    ]


#: The complete findings list of every fixture above, as
#: ``(severity, code, message, nodeId, connectionId)`` in emission order,
#: captured from the PRE-FEATURE validator and unchanged by this feature
#: (Requirement 2.7). Regenerate only when a pre-feature rule intentionally
#: changes — never to accommodate a new stream or analytics rule leaking
#: into a stream-free graph.
_PRE_FEATURE_GOLDENS = {
    "aravis_and_custom_python_source": [
        (
            "error", "V7_COEXISTENCE_CONFLICT",
            "Node 'a1': frame-feed source nodes ('a1', 'py') cannot "
            "coexist in one workflow: the runtime serves one frame-feed"
            " source per workflow",
            "a1", None,
        ),
        (
            "error", "V7_COEXISTENCE_CONFLICT",
            "Node 'py': frame-feed source nodes ('a1', 'py') cannot "
            "coexist in one workflow: the runtime serves one frame-feed"
            " source per workflow",
            "py", None,
        ),
    ],
    "coerced_inference_to_capture": [],
    "connection_targets_a_trigger": [
        (
            "error", "V2_UNKNOWN_PORT",
            "Connection 'bad' target references unknown port 'in' on "
            "node 'din'",
            None, "bad",
        ),
        (
            "error", "V5_UNREACHABLE_NODE",
            "Node 'cap' is not reachable from any input node",
            "cap", None,
        ),
        (
            "warning", "W1_UNUSED_OUTPUT_PORT",
            "Output port 'out' of node 'din' is not connected",
            "din", None,
        ),
        (
            "warning", "W1_OUTPUT_NODE_NO_INPUT",
            "Output node 'cap' has no incoming connection",
            "cap", None,
        ),
        (
            "error", "V7_STAGE_ORDER",
            "Connection 'bad' targets trigger node 'din': a trigger may"
            " not be downstream of any node (Trigger -> Input ordering)",
            None, "bad",
        ),
    ],
    "detached_node": [
        (
            "error", "V5_UNREACHABLE_NODE",
            "Node 'stray' is not reachable from any input node",
            "stray", None,
        ),
        (
            "warning", "W1_UNUSED_OUTPUT_PORT",
            "Output port 'out' of node 'stray' is not connected",
            "stray", None,
        ),
    ],
    "empty_graph": [
        (
            "error", "V1_NO_INPUT_NODE",
            "Workflow must contain at least one input node",
            None, None,
        ),
        (
            "error", "V1_NO_OUTPUT_NODE",
            "Workflow must contain at least one output node",
            None, None,
        ),
    ],
    "incompatible_port_types": [
        (
            "error", "V2_INCOMPATIBLE_TYPES",
            "Connection 'bad': Cannot connect EventSignal output to "
            "VideoFrames input",
            None, "bad",
        ),
        (
            "error", "V5_UNREACHABLE_NODE",
            "Node 'cap' is not reachable from any input node",
            "cap", None,
        ),
        (
            "warning", "W1_UNUSED_OUTPUT_PORT",
            "Output port 'out' of node 'rot' is not connected",
            "rot", None,
        ),
        (
            "warning", "W1_OUTPUT_NODE_NO_INPUT",
            "Output node 'cap' has no incoming connection",
            "cap", None,
        ),
    ],
    "invalid_parameter_value": [
        (
            "error", "V4_INVALID_PARAMETER_VALUE",
            "Node 'cap': Parameter 'quality' value 200 is above the "
            "maximum 100",
            "cap", None,
        ),
    ],
    "many_defect_classes": [
        (
            "error", "V1_NO_INPUT_NODE",
            "Workflow must contain at least one input node",
            None, None,
        ),
        (
            "error", "V2_INCOMPATIBLE_TYPES",
            "Connection 'c3': Cannot connect VideoFrames output to "
            "InferenceMeta input",
            None, "c3",
        ),
        (
            "error", "V3_CYCLE",
            "Node 'rotA' participates in a cycle with nodes: rotA, rotB",
            "rotA", None,
        ),
        (
            "error", "V3_CYCLE",
            "Node 'rotB' participates in a cycle with nodes: rotA, rotB",
            "rotB", None,
        ),
        (
            "error", "V4_MISSING_REQUIRED_PARAMETER",
            "Node 'filt': Required parameter 'condition' has no value",
            "filt", None,
        ),
        (
            "error", "V5_UNREACHABLE_NODE",
            "Node 'rotA' is not reachable from any input node",
            "rotA", None,
        ),
        (
            "error", "V5_UNREACHABLE_NODE",
            "Node 'rotB' is not reachable from any input node",
            "rotB", None,
        ),
        (
            "error", "V5_UNREACHABLE_NODE",
            "Node 'filt' is not reachable from any input node",
            "filt", None,
        ),
        (
            "error", "V5_UNREACHABLE_NODE",
            "Node 'stray' is not reachable from any input node",
            "stray", None,
        ),
        (
            "error", "V5_UNREACHABLE_NODE",
            "Node 'cap' is not reachable from any input node",
            "cap", None,
        ),
        (
            "warning", "W1_UNUSED_OUTPUT_PORT",
            "Output port 'out' of node 'filt' is not connected",
            "filt", None,
        ),
        (
            "warning", "W1_UNUSED_OUTPUT_PORT",
            "Output port 'out' of node 'stray' is not connected",
            "stray", None,
        ),
        (
            "warning", "W1_OUTPUT_NODE_NO_INPUT",
            "Output node 'cap' has no incoming connection",
            "cap", None,
        ),
    ],
    "missing_required_parameter": [
        (
            "error", "V4_MISSING_REQUIRED_PARAMETER",
            "Node 'src2': Required parameter 'location' has no value",
            "src2", None,
        ),
    ],
    "no_input_node": [
        (
            "error", "V1_NO_INPUT_NODE",
            "Workflow must contain at least one input node",
            None, None,
        ),
        (
            "error", "V5_UNREACHABLE_NODE",
            "Node 'cap' is not reachable from any input node",
            "cap", None,
        ),
        (
            "warning", "W1_OUTPUT_NODE_NO_INPUT",
            "Output node 'cap' has no incoming connection",
            "cap", None,
        ),
    ],
    "no_output_node": [
        (
            "error", "V1_NO_OUTPUT_NODE",
            "Workflow must contain at least one output node",
            None, None,
        ),
        (
            "warning", "W1_UNUSED_OUTPUT_PORT",
            "Output port 'out' of node 'src' is not connected",
            "src", None,
        ),
    ],
    "single_aravis_source": [],
    "subscription_trigger_activation_connected": [],
    "subscription_trigger_activation_unconnected": [
        (
            "warning", "W1_UNUSED_OUTPUT_PORT",
            "Output port 'out' of node 'msub' is not connected",
            "msub", None,
        ),
        (
            "error", "V9_MIXED_ACTIVATION_MODEL",
            "Input node 'src' has no trigger connected to its "
            "'activation' port: a workflow with subscription triggers "
            "must drive every input from a trigger",
            "src", None,
        ),
    ],
    "two_aravis_sources": [
        (
            "error", "V5_UNREACHABLE_NODE",
            "Node 'cap' is not reachable from any input node",
            "cap", None,
        ),
        (
            "error", "V7_COEXISTENCE_CONFLICT",
            "Node 'a1': 2 nodes of type 'aravis_camera_source' cannot "
            "coexist in one workflow ('a1', 'a2'): the single-frame "
            "appsrc feed supports exactly one Aravis camera source per "
            "workflow",
            "a1", None,
        ),
        (
            "error", "V7_COEXISTENCE_CONFLICT",
            "Node 'a2': 2 nodes of type 'aravis_camera_source' cannot "
            "coexist in one workflow ('a1', 'a2'): the single-frame "
            "appsrc feed supports exactly one Aravis camera source per "
            "workflow",
            "a2", None,
        ),
        (
            "warning", "W1_UNUSED_OUTPUT_PORT",
            "Output port 'out' of node 'a1' is not connected",
            "a1", None,
        ),
        (
            "warning", "W1_UNUSED_OUTPUT_PORT",
            "Output port 'out' of node 'a2' is not connected",
            "a2", None,
        ),
        (
            "warning", "W1_OUTPUT_NODE_NO_INPUT",
            "Output node 'cap' has no incoming connection",
            "cap", None,
        ),
    ],
    "two_custom_python_sources": [
        (
            "error", "V5_UNREACHABLE_NODE",
            "Node 'cap' is not reachable from any input node",
            "cap", None,
        ),
        (
            "error", "V7_COEXISTENCE_CONFLICT",
            "Node 'py1': 2 nodes of type 'custom_python_source' cannot "
            "coexist in one workflow ('py1', 'py2'): the single-frame "
            "appsrc feed serves exactly one frame-feed source per "
            "workflow",
            "py1", None,
        ),
        (
            "error", "V7_COEXISTENCE_CONFLICT",
            "Node 'py2': 2 nodes of type 'custom_python_source' cannot "
            "coexist in one workflow ('py1', 'py2'): the single-frame "
            "appsrc feed serves exactly one frame-feed source per "
            "workflow",
            "py2", None,
        ),
        (
            "warning", "W1_UNUSED_OUTPUT_PORT",
            "Output port 'out' of node 'py1' is not connected",
            "py1", None,
        ),
        (
            "warning", "W1_UNUSED_OUTPUT_PORT",
            "Output port 'out' of node 'py2' is not connected",
            "py2", None,
        ),
        (
            "warning", "W1_OUTPUT_NODE_NO_INPUT",
            "Output node 'cap' has no incoming connection",
            "cap", None,
        ),
    ],
    "two_node_cycle": [
        (
            "error", "V3_CYCLE",
            "Node 'rotA' participates in a cycle with nodes: rotA, rotB",
            "rotA", None,
        ),
        (
            "error", "V3_CYCLE",
            "Node 'rotB' participates in a cycle with nodes: rotA, rotB",
            "rotB", None,
        ),
    ],
    "unified_folder_input_with_trigger": [],
    "unknown_node_type": [
        (
            "error", "UNKNOWN_NODE_TYPE",
            "Node 'mystery' has unknown type 'not_a_real_type'",
            "mystery", None,
        ),
        (
            "error", "V5_UNREACHABLE_NODE",
            "Node 'mystery' is not reachable from any input node",
            "mystery", None,
        ),
    ],
    "valid_folder_capture": [],
}


class TestPreFeatureFixtureGoldens:
    """Requirement 2.7: a workflow with no Stream_Camera_Source_Node and no
    Scene_Analytics_Node gets exactly the findings it got before this
    feature."""

    def test_every_fixture_has_a_golden(self):
        assert sorted(_fixtures()) == sorted(_PRE_FEATURE_GOLDENS), (
            "each fixture needs a golden; regenerate deliberately")

    def test_fixture_findings_match_their_goldens(self):
        for name, graph in sorted(_fixtures().items()):
            actual = _finding_tuples(validate(graph))
            expected = [tuple(entry) for entry in _PRE_FEATURE_GOLDENS[name]]
            assert actual == expected, name

    def test_no_new_rule_fires_on_any_fixture(self):
        """The literal reading of Requirement 2.7: none of the feature's
        four new codes appears on a stream-free, analytics-free graph."""
        new_codes = {
            CODE_V11_STREAM_URL,
            CODE_V12_CONTINUOUS_ACTIVATION,
            CODE_V13_ANALYTICS_CONFIG_INVALID,
            CODE_W3_ANALYTICS_NO_DETECTOR,
        }
        for name, graph in sorted(_fixtures().items()):
            fired = {f.code for f in validate(graph)} & new_codes
            assert fired == set(), (name, sorted(fired))

    def test_the_corpus_covers_the_rules_this_feature_touched(self):
        """Guard against a corpus that silently stops exercising V7
        coexistence or V9 — the two pre-feature rules tasks 3.1 and 3.2
        modified."""
        codes = set()
        for graph in _fixtures().values():
            codes.update(f.code for f in validate(graph))
        assert CODE_V7_COEXISTENCE_CONFLICT in codes
        assert "V9_MIXED_ACTIVATION_MODEL" in codes
