"""Unit tests for workflow_core.scaffold (Plugin_Scaffold rendering and
validation, Requirements 1.2, 1.3, 1.4, 1.5).

Covers: rendered file map completeness (hook file, C skeleton, one
meson.build per selected Target_Architecture, README), the
process_frame(frame, params) hook contract, declared parameters surfacing
as GObject properties plumbed into the params dict, generation failures
identifying the failing input, and scaffold validation rejecting
non-buildable source (missing hook file, missing build configurations,
empty required files) with a description of the failure.
"""

import pytest

from workflow_core.catalog import ARCH_ARM64_JP5, ARCH_X86_64
from workflow_core.scaffold import (
    HOOK_FILE,
    README_FILE,
    ScaffoldError,
    build_config_path,
    c_source_path,
    element_name_for,
    plugin_ident_for,
    render_scaffold,
    scaffold_defects,
    validate_scaffold,
)


def make_declaration(**overrides):
    """A valid Custom_Node_Type declaration in the wire shape accepted by
    descriptor_from_declaration, plus the selected architectures."""
    declaration = {
        "typeId": "custom_blur",
        "displayName": "Custom Blur",
        "description": "Blurs each frame with a configurable radius.",
        "category": "preprocessing",
        "inputs": [{"name": "in", "portType": "VideoFrames"}],
        "outputs": [{"name": "out", "portType": "VideoFrames"}],
        "parameters": [
            {
                "name": "radius",
                "paramType": "int",
                "required": True,
                "default": 3,
                "constraints": {"min": 1, "max": 99},
                "description": "Blur radius in pixels.",
                "examples": [3],
            },
            {
                "name": "mode",
                "paramType": "enum",
                "required": False,
                "default": "gaussian",
                "constraints": {"values": ["gaussian", "box"]},
                "description": "Blur kernel to apply.",
                "examples": ["gaussian"],
            },
            {
                "name": "normalize",
                "paramType": "bool",
                "required": False,
                "default": False,
                "description": "Normalize output intensity.",
                "examples": [True],
            },
            {
                "name": "strength",
                "paramType": "float",
                "required": False,
                "default": 0.5,
                "constraints": {"min": 0.0, "max": 1.0},
                "description": "Blend strength between input and output.",
                "examples": [0.5],
            },
        ],
        "architectures": [ARCH_X86_64, ARCH_ARM64_JP5],
    }
    declaration.update(overrides)
    return declaration


class TestRenderScaffold:
    def test_renders_expected_file_map(self):
        declaration = make_declaration()
        files = render_scaffold(declaration)
        assert set(files) == {
            HOOK_FILE,
            c_source_path(declaration),
            README_FILE,
            build_config_path(ARCH_X86_64),
            build_config_path(ARCH_ARM64_JP5),
        }
        for path, content in files.items():
            assert isinstance(content, str) and content.strip(), path

    def test_hook_exposes_process_frame_contract(self):
        hook = render_scaffold(make_declaration())[HOOK_FILE]
        assert "def process_frame(frame, params):" in hook
        assert "return frame" in hook

    def test_hook_lists_every_declared_parameter(self):
        hook = render_scaffold(make_declaration())[HOOK_FILE]
        for name in ("radius", "mode", "normalize", "strength"):
            assert name in hook

    def test_parameters_surface_as_gobject_properties(self):
        declaration = make_declaration()
        c_source = render_scaffold(declaration)[c_source_path(declaration)]
        # one property installed per declared parameter, typed by paramType
        assert c_source.count("g_object_class_install_property") == 4
        assert 'g_param_spec_int ("radius"' in c_source
        assert 'g_param_spec_string ("mode"' in c_source
        assert 'g_param_spec_boolean ("normalize"' in c_source
        assert 'g_param_spec_double ("strength"' in c_source
        # every declared name is plumbed into the hook's params dict
        for name in ("radius", "mode", "normalize", "strength"):
            assert 'PyDict_SetItemString (params, "{0}"'.format(name) in c_source

    def test_c_skeleton_bridges_appsink_appsrc_to_hook(self):
        declaration = make_declaration()
        c_source = render_scaffold(declaration)[c_source_path(declaration)]
        assert "appsink" in c_source and "appsrc" in c_source
        assert '"process_frame"' in c_source
        assert 'gst_element_register (plugin, "customblur"' in c_source

    def test_c_skeleton_avoids_version_gated_apis(self):
        # The C skeleton must link against every supported device stack:
        # JetPack 4 ships GStreamer 1.14 / glib 2.56, JetPack 5 ships
        # GStreamer 1.16. Symbols introduced after those versions must not
        # appear anywhere in the rendered scaffold.
        version_gated_symbols = (
            "gst_buffer_new_memdup",           # GStreamer >= 1.20
            "g_memdup2",                       # glib >= 2.68
            "gst_element_request_pad_simple",  # GStreamer >= 1.20
        )
        files = render_scaffold(make_declaration())
        for path, content in files.items():
            for symbol in version_gated_symbols:
                assert symbol not in content, (
                    "{0} references version-gated symbol {1}".format(
                        path, symbol))

    def test_one_build_configuration_per_selected_architecture(self):
        declaration = make_declaration(
            architectures=[ARCH_X86_64, ARCH_ARM64_JP5])
        files = render_scaffold(declaration)
        for arch in (ARCH_X86_64, ARCH_ARM64_JP5):
            meson = files[build_config_path(arch)]
            assert "project('gst-customblur'" in meson
            assert arch in meson

    def test_readme_documents_parameters_and_architectures(self):
        files = render_scaffold(make_declaration())
        readme = files[README_FILE]
        assert "process_frame" in readme
        for name in ("radius", "mode", "normalize", "strength"):
            assert name in readme
        assert ARCH_X86_64 in readme and ARCH_ARM64_JP5 in readme

    def test_no_parameters_declared(self):
        declaration = make_declaration(parameters=[])
        files = render_scaffold(declaration)
        c_source = files[c_source_path(declaration)]
        assert "g_object_class_install_property" not in c_source
        assert "def process_frame(frame, params):" in files[HOOK_FILE]


class TestRenderScaffoldFailures:
    def test_invalid_category_identifies_field(self):
        with pytest.raises(ScaffoldError) as exc_info:
            render_scaffold(make_declaration(category="nonsense"))
        assert exc_info.value.field == "category"

    def test_invalid_port_type_identifies_field(self):
        with pytest.raises(ScaffoldError) as exc_info:
            render_scaffold(make_declaration(
                inputs=[{"name": "in", "portType": "Bogus"}]))
        assert exc_info.value.field == "inputs[0].portType"

    def test_empty_architectures_rejected(self):
        with pytest.raises(ScaffoldError) as exc_info:
            render_scaffold(make_declaration(architectures=[]))
        assert exc_info.value.field == "architectures"

    def test_unknown_architecture_rejected(self):
        with pytest.raises(ScaffoldError) as exc_info:
            render_scaffold(make_declaration(architectures=["riscv"]))
        assert exc_info.value.field == "architectures[0]"

    def test_duplicate_architecture_rejected(self):
        with pytest.raises(ScaffoldError) as exc_info:
            render_scaffold(make_declaration(
                architectures=[ARCH_X86_64, ARCH_X86_64]))
        assert exc_info.value.field == "architectures[1]"

    def test_architectures_fall_back_to_mappings(self):
        declaration = make_declaration(architectures=None)
        del declaration["architectures"]
        declaration["mappings"] = [
            {"arch": ARCH_X86_64, "elementChain": [], "pluginDependencies": []}
        ]
        files = render_scaffold(declaration)
        assert build_config_path(ARCH_X86_64) in files


class TestElementName:
    def test_derived_from_type_id(self):
        assert element_name_for({"typeId": "custom_blur"}) == "customblur"
        assert element_name_for({"typeId": "My-Node.2"}) == "mynode2"

    def test_leading_digit_gets_prefixed(self):
        assert element_name_for({"typeId": "3dfilter"})[0].isalpha()

    def test_unusable_type_id_rejected(self):
        with pytest.raises(ScaffoldError) as exc_info:
            element_name_for({"typeId": "---"})
        assert exc_info.value.field == "typeId"


class TestPluginIdent:
    """plugin_ident_for mirrors the promoted-artifact filename-to-symbol
    derivation (sanitize_plugin_name of displayName, then GStreamer's
    extract_symname): a mismatch makes the loader silently reject the
    built library (custom-node-plugin-runtime-fixes, defect 1)."""

    def test_mirrors_sanitized_display_name(self):
        # displayName 'resize image' promotes as 'resize-image.so';
        # GStreamer derives 'resize_image' from that filename.
        ident = plugin_ident_for(
            {"typeId": "custom.resize_image", "displayName": "resize image"})
        assert ident == "resize_image"

    def test_strips_gstreamer_library_prefixes(self):
        # extract_symname strips libgst/lib/gst before deriving the ident.
        ident = plugin_ident_for(
            {"typeId": "gstblur", "displayName": "gstblur"})
        assert ident == "blur"

    def test_falls_back_to_element_name_without_display_name(self):
        declaration = {"typeId": "custom_blur"}
        assert plugin_ident_for(declaration) == \
            element_name_for(declaration)

    def test_c_skeleton_plugin_define_and_meson_agree_on_ident(self):
        # GST_PLUGIN_DEFINE token, PACKAGE, and the meson library base
        # name must all carry the same ident so the built
        # libgst<ident>.so AND the promoted {plugin}.so both resolve
        # gst_plugin_<ident>_get_desc.
        declaration = make_declaration(displayName="resize image")
        files = render_scaffold(declaration)
        ident = plugin_ident_for(declaration)
        assert ident == "resize_image"
        c_source = files[c_source_path(declaration)]
        assert "GST_PLUGIN_DEFINE (GST_VERSION_MAJOR, GST_VERSION_MINOR,\n" \
               "    {0},".format(ident) in c_source
        assert '#define PACKAGE "{0}"'.format(ident) in c_source
        meson = files[build_config_path(ARCH_X86_64)]
        assert "shared_library('gst{0}'".format(ident) in meson
        # the element factory name is independent of the plugin ident
        assert 'gst_element_register (plugin, "customblur"' in c_source


class TestRuntimeBridgeHardening:
    """On-device verified fixes to the appsink/appsrc bridge
    (custom-node-plugin-runtime-fixes; LocalServer v1.0.56 on
    ryan-orin-nano/JP6)."""

    def c_source(self, **overrides):
        declaration = make_declaration(**overrides)
        return render_scaffold(declaration)[c_source_path(declaration)]

    def test_preroll_sample_is_processed(self):
        # Without a new-preroll handler the FIRST buffer is never pushed
        # and the whole pipeline hangs in PREROLLING until the watchdog.
        c_source = self.c_source()
        assert 'g_signal_connect (self->appsink, "new-preroll"' in c_source
        assert "gst_app_sink_pull_preroll" in c_source

    def test_eos_is_forwarded(self):
        # Bounded (single-frame) runs must terminate instead of hanging.
        c_source = self.c_source()
        assert 'g_signal_connect (self->appsink, "eos"' in c_source
        assert "gst_app_src_end_of_stream" in c_source

    def test_output_caps_are_synced_to_appsrc(self):
        c_source = self.c_source()
        assert "gst_app_src_set_caps" in c_source

    def test_gil_released_before_push(self):
        # Holding the GIL across gst_app_src_push_buffer deadlocks the
        # embedding LocalServer process.
        c_source = self.c_source()
        release = c_source.index("PyGILState_Release (gil);\n"
                                 "    gst_buffer_unmap")
        push = c_source.index("gst_app_src_push_buffer")
        assert release < push

    def test_interpreter_init_releases_gil(self):
        # Py_InitializeEx leaves the caller holding the GIL; without
        # PyEval_SaveThread the streaming thread deadlocks on Ensure.
        c_source = self.c_source()
        assert "PyEval_SaveThread" in c_source

    def test_hook_import_self_locates_via_dladdr(self):
        # The hook travels beside the .so; sys.path must gain that
        # directory before the import, in any host process.
        c_source = self.c_source()
        assert "dladdr" in c_source
        assert "PyList_Insert (sys_path, 0, dir)" in c_source
        assert c_source.index("PyList_Insert") < c_source.index(
            'PyImport_ImportModule ("frame_processing_hook")')

    def test_appsrc_uses_time_format(self):
        c_source = self.c_source()
        assert 'g_object_set (self->appsrc, "format", GST_FORMAT_TIME' \
            in c_source

    def test_frame_geometry_rides_in_params(self):
        # The negotiated caps geometry is injected so hooks never guess
        # the incoming width/height/pixel format.
        declaration = make_declaration()
        files = render_scaffold(declaration)
        c_source = files[c_source_path(declaration)]
        for key in ("frame_width", "frame_height", "frame_format"):
            assert 'PyDict_SetItemString (params, "{0}"'.format(key) \
                in c_source
            assert key in files[HOOK_FILE]
            assert key in files[README_FILE]


class TestValidateScaffold:
    def test_accepts_rendered_scaffold(self):
        declaration = make_declaration()
        files = render_scaffold(declaration)
        assert validate_scaffold(files, declaration) is None
        assert scaffold_defects(files, declaration) == []

    def test_rejects_missing_hook_file(self):
        declaration = make_declaration()
        files = render_scaffold(declaration)
        del files[HOOK_FILE]
        with pytest.raises(ScaffoldError) as exc_info:
            validate_scaffold(files, declaration)
        assert "Frame_Processing_Hook" in str(exc_info.value)
        assert HOOK_FILE in str(exc_info.value)

    def test_rejects_missing_build_configuration(self):
        declaration = make_declaration()
        files = render_scaffold(declaration)
        del files[build_config_path(ARCH_ARM64_JP5)]
        with pytest.raises(ScaffoldError) as exc_info:
            validate_scaffold(files, declaration)
        assert ARCH_ARM64_JP5 in str(exc_info.value)

    def test_rejects_all_build_configurations_missing(self):
        declaration = make_declaration()
        files = render_scaffold(declaration)
        del files[build_config_path(ARCH_X86_64)]
        del files[build_config_path(ARCH_ARM64_JP5)]
        with pytest.raises(ScaffoldError) as exc_info:
            validate_scaffold(files, declaration)
        message = str(exc_info.value)
        assert ARCH_X86_64 in message and ARCH_ARM64_JP5 in message

    def test_rejects_empty_required_file(self):
        declaration = make_declaration()
        for path in (HOOK_FILE, c_source_path(declaration),
                     build_config_path(ARCH_X86_64)):
            files = render_scaffold(declaration)
            files[path] = "   \n"
            with pytest.raises(ScaffoldError) as exc_info:
                validate_scaffold(files, declaration)
            assert "empty" in str(exc_info.value)
            assert path in str(exc_info.value)

    def test_collects_every_defect_with_descriptions(self):
        declaration = make_declaration()
        files = render_scaffold(declaration)
        del files[HOOK_FILE]
        files[c_source_path(declaration)] = ""
        defects = scaffold_defects(files, declaration)
        assert len(defects) == 2
        assert all(isinstance(d, str) and d for d in defects)

    def test_missing_readme_does_not_fail_buildability(self):
        declaration = make_declaration()
        files = render_scaffold(declaration)
        del files[README_FILE]
        assert validate_scaffold(files, declaration) is None

    def test_rejects_non_file_map(self):
        declaration = make_declaration()
        defects = scaffold_defects(["not", "a", "map"], declaration)
        assert defects and "file map" in defects[0]
