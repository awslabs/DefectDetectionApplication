"""Launch-string rendering of the Compiled Pipeline Document — must
match the dialect LocalServer executes (design section 5): " ! " joins,
`t0. ! queue ! ...` tee branches, `... ! f0.` funnel links."""

from harness import renderer


def _element(factory, node_id=None, **args):
    return {"nodeId": node_id, "factory": factory, "args": args}


def _segment(name, elements, from_ref=None, link_to=None):
    return {"name": name, "from": from_ref, "linkTo": link_to,
            "elements": elements}


LINEAR_DOCUMENT = {
    "segments": [
        _segment("s0", [
            _element("multifilesrc", "cam", location="/tmp/ds/frame_%05d.jpg"),
            _element("jpegparse", "cam"),
            _element("jpegdec", "cam", **{"idct-method": 2}),
            _element("videoconvert", "cam"),
            _element("capsfilter", "inf", caps="video/x-raw,format=RGB"),
            _element("emltriton", "inf",
                     **{"model-repo": "/aws_dda/dda_triton/triton_model_repo",
                        "model": "defect-model"}),
        ]),
    ],
    "executorBindings": [],
}

BRANCHED_DOCUMENT = {
    "segments": [
        _segment("s0", [
            _element("videotestsrc", "src"),
            {"nodeId": None, "factory": "tee", "args": {"name": "t0"}},
        ]),
        _segment("s1", [
            {"nodeId": None, "factory": "queue", "args": {}},
            _element("videoflip", "rot", method="clockwise"),
        ], from_ref="t0", link_to="f0"),
        _segment("s2", [
            {"nodeId": None, "factory": "queue", "args": {}},
            _element("videocrop", "crop", top=0, bottom=0, left=0, right=0),
        ], from_ref="t0", link_to="f0"),
        _segment("s3", [
            {"nodeId": None, "factory": "funnel", "args": {"name": "f0"}},
            _element("videoconvert", "sink"),
        ]),
    ],
    "executorBindings": [
        {"nodeId": "mq", "binding": "recording_mqtt_publish",
         "parameters": {"topic": "a/b"}, "upstreamNodeIds": ["sink"],
         "downstreamNodeIds": []},
    ],
}


class TestRenderLaunchString:
    def test_linear_segment_joins_elements_with_bang(self):
        rendered = renderer.render_launch_string(LINEAR_DOCUMENT)
        assert rendered == (
            "multifilesrc location=/tmp/ds/frame_%05d.jpg ! jpegparse ! "
            "jpegdec idct-method=2 ! videoconvert ! "
            "capsfilter caps=video/x-raw,format=RGB ! "
            "emltriton model-repo=/aws_dda/dda_triton/triton_model_repo "
            "model=defect-model"
        )

    def test_tee_branches_and_funnel_links(self):
        rendered = renderer.render_launch_string(BRANCHED_DOCUMENT)
        assert rendered == (
            "videotestsrc ! tee name=t0 "
            "t0. ! queue ! videoflip method=clockwise ! f0. "
            "t0. ! queue ! videocrop top=0 bottom=0 left=0 right=0 ! f0. "
            "funnel name=f0 ! videoconvert"
        )

    def test_boolean_args_render_lowercase(self):
        document = {"segments": [_segment("s0", [
            _element("identity", "n1", sync=True, silent=False),
        ])]}
        assert renderer.render_launch_string(document) == \
            "identity sync=true silent=false"

    def test_empty_segments_are_skipped(self):
        document = {"segments": [
            _segment("s0", [_element("fakesrc", "a")]),
            _segment("s1", []),
        ]}
        assert renderer.render_launch_string(document) == "fakesrc"

    def test_empty_string_arg_is_quoted_not_bare(self):
        # A bare `meta=` makes Gst.parse_launch read `meta` as an
        # element name and fail with 'no element "meta"' — the
        # emlcapture regression behind the workflow test failures.
        document = {"segments": [_segment("s0", [
            _element("emlcapture", "n3",
                     **{"buffer-message-id": "file-target", "meta": ""}),
        ])]}
        assert renderer.render_launch_string(document) == \
            'emlcapture buffer-message-id=file-target meta=""'

    def test_values_with_launch_syntax_are_quoted_and_escaped(self):
        assert renderer.render_value("with space") == '"with space"'
        assert renderer.render_value("a!b") == '"a!b"'
        assert renderer.render_value('say "hi"') == '"say \\"hi\\""'

    def test_plain_tokens_stay_unquoted(self):
        # Caps strings, paths, and numbers keep their existing bare form.
        assert renderer.render_value("video/x-raw,format=RGB") == \
            "video/x-raw,format=RGB"
        assert renderer.render_value("/tmp/out_%05d.jpg") == \
            "/tmp/out_%05d.jpg"
        assert renderer.render_value(85) == "85"


class TestElementNameMap:
    def test_auto_names_use_per_factory_counters(self):
        name_map = renderer.element_name_map(LINEAR_DOCUMENT)
        assert name_map["multifilesrc0"] == "cam"
        assert name_map["jpegdec0"] == "cam"
        assert name_map["emltriton0"] == "inf"

    def test_explicit_names_override_and_still_count(self):
        document = {"segments": [_segment("s0", [
            {"nodeId": None, "factory": "tee", "args": {"name": "t0"}},
            _element("tee", "n2"),
        ])]}
        name_map = renderer.element_name_map(document)
        assert name_map["t0"] is None          # synthetic
        assert name_map["tee1"] == "n2"        # counter includes named tee

    def test_failing_node_lookup(self):
        name_map = renderer.element_name_map(BRANCHED_DOCUMENT)
        assert renderer.node_id_for_element(name_map, "videocrop0") == "crop"
        assert renderer.node_id_for_element(name_map, "queue0") is None
        assert renderer.node_id_for_element(name_map, "unknown9") is None


class TestNodeCollection:
    def test_gst_node_ids_in_document_order_once(self):
        assert renderer.gst_node_ids(BRANCHED_DOCUMENT) == \
            ["src", "rot", "crop", "sink"]

    def test_all_node_ids_include_executor_bindings(self):
        assert renderer.all_node_ids(BRANCHED_DOCUMENT) == \
            ["src", "rot", "crop", "sink", "mq"]

    def test_contiguous_chain_counts_once(self):
        assert renderer.gst_node_ids(LINEAR_DOCUMENT) == ["cam", "inf"]

    def test_nodes_with_factory(self):
        assert renderer.nodes_with_factory(LINEAR_DOCUMENT, "emltriton") == ["inf"]
        assert renderer.nodes_with_factory(LINEAR_DOCUMENT, "tee") == []


class TestPlaceholderResolution:
    def test_dataset_location_resolved_in_place(self):
        document = {"segments": [_segment("s0", [
            _element("multifilesrc", "cam", location="{dataset_location}"),
            _element("jpegparse", "cam"),
        ])]}
        count = renderer.resolve_placeholder(
            document, "dataset_location", "/tmp/ds/frame_%05d.jpg")
        assert count == 1
        assert document["segments"][0]["elements"][0]["args"]["location"] == \
            "/tmp/ds/frame_%05d.jpg"

    def test_non_string_args_untouched(self):
        document = {"segments": [_segment("s0", [
            _element("jpegdec", "cam", **{"idct-method": 2}),
        ])]}
        assert renderer.resolve_placeholder(document, "dataset_location", "x") == 0


class TestSimAppsrcNames:
    def test_sim_sources_detected(self):
        document = {"segments": [_segment("s0", [
            _element("appsrc", "din", name="sim_source_din"),
            _element("appsrc", "cam2", name="appsrc"),  # not a sim stub
        ])]}
        assert renderer.sim_appsrc_names(document) == [("sim_source_din", "din")]


# --------------------------------------------------------------------------
# Hardware sink stubbing (emlcapture -> fakesink) for the CPU-only sandbox
# --------------------------------------------------------------------------

def _capture_document():
    """A dataset -> capture pipeline: the compiler emits a real
    ``jpegenc ! emlcapture`` chain for the ``capture`` node even in
    simulation (the node is hardware_dependent=False)."""
    return {
        "segments": [
            _segment("s0", [
                _element("multifilesrc", "cam",
                         location="/tmp/ds/frame_%05d.jpg"),
                _element("jpegparse", "cam"),
                _element("jpegdec", "cam", **{"idct-method": 2}),
                _element("videoconvert", "cap"),
                _element("jpegenc", "cap", **{"idct-method": 2, "quality": 100}),
                _element("emlcapture", "cap",
                         **{"buffer-message-id": "file-target_/aws_dda/captures-jpg",
                            "interval": 0, "meta": ""}),
            ]),
        ],
        "executorBindings": [],
    }


def test_stub_hardware_sinks_rewrites_emlcapture_to_fakesink():
    document = _capture_document()

    stubbed = renderer.stub_hardware_sinks(document)

    assert stubbed == ["cap"]
    sink = document["segments"][0]["elements"][-1]
    assert sink["factory"] == "fakesink"
    assert sink["nodeId"] == "cap"
    assert sink["args"] == {"sync": False, "name": "sim_capture_cap"}
    # The emlcapture-specific args are dropped (fakesink rejects them).
    assert "buffer-message-id" not in sink["args"]

    # The rewritten pipeline renders as a valid launch string ending in
    # the benign sink, and the sink name maps back to the capture node.
    launch = renderer.render_launch_string(document)
    assert launch.endswith("fakesink sync=false name=sim_capture_cap")
    name_map = renderer.element_name_map(document)
    assert renderer.node_id_for_element(name_map, "sim_capture_cap") == "cap"


def test_stub_hardware_sinks_noop_without_hardware_sink():
    document = dict(LINEAR_DOCUMENT)
    before = renderer.render_launch_string(document)

    stubbed = renderer.stub_hardware_sinks(document)

    assert stubbed == []
    assert renderer.render_launch_string(document) == before
