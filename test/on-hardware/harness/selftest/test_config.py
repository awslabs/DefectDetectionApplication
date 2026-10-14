"""Unit tests for harnesslib.config (Reqs 1.1, 1.2, 2.1).

Covers file+env merge precedence, fail-closed validation of architectures and
capability names, credential reference parsing (values never read into config,
never echoed in errors), timeout defaults/overrides, and the stream camera
stage's inputs (credential-free URLs, fail-closed failure categories, the
file-only failure mapping, and the redacted stream credentials).
"""

import dataclasses
import json
from pathlib import Path

import pytest
from harnesslib.config import (
    KNOWN_CAPABILITIES,
    KNOWN_STREAM_FAILURE_CATEGORIES,
    CredentialRef,
    DeviceProfile,
    DeviceTarget,
    ExpectedComponents,
    HarnessConfigError,
    SecretStr,
    StreamCredentials,
    Timeouts,
    load_config,
    resolve_stream_credentials,
    stream_source_type,
)

BASE_YAML = """
devices:
  jp6-orinagx:
    base_url: http://localhost:5000
    profile:
      architecture: arm64_jp6
      capabilities: [vllm, onnx_models, workflows]
    credentials: env:DDA_HARNESS_TOKEN
    expected:
      vision_models: [model-a, model-b]
      vllm_models: [opt125m-smoke]
      workflows: []
    timeouts:
      vllm_ready_s: 600
  jp5-xavier:
    base_url: http://192.168.1.42:5000
    profile:
      architecture: arm64_jp5
      capabilities: [dlr_models, onnx_models]
"""


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "devices.yaml"
    path.write_text(BASE_YAML)
    return path


def load(config_file, env):
    return load_config(config_path=config_file, environ=env)


class TestFileLoadingAndSelection:
    def test_selected_device_loaded_from_file(self, config_file):
        target = load(config_file, {"DDA_HARNESS_DEVICE": "jp6-orinagx"})
        assert target.name == "jp6-orinagx"
        assert target.base_url == "http://localhost:5000"
        assert target.profile.architecture == "arm64_jp6"
        assert target.profile.capabilities == frozenset({"vllm", "onnx_models", "workflows"})
        assert target.expected.vision_models == ("model-a", "model-b")
        assert target.expected.vllm_models == ("opt125m-smoke",)
        assert target.expected.workflows == ()

    def test_unknown_device_name_lists_available(self, config_file):
        with pytest.raises(HarnessConfigError, match="jp5-xavier"):
            load(config_file, {"DDA_HARNESS_DEVICE": "nope"})

    def test_multiple_devices_without_selection_rejected(self, config_file):
        with pytest.raises(HarnessConfigError, match="DDA_HARNESS_DEVICE"):
            load(config_file, {})

    def test_single_device_file_needs_no_selection(self, tmp_path):
        path = tmp_path / "devices.yaml"
        path.write_text(
            "devices:\n"
            "  only-one:\n"
            "    base_url: http://h:5000\n"
            "    profile: {architecture: x86_64}\n"
        )
        assert load(path, {}).name == "only-one"

    def test_base_url_trailing_slash_normalized(self, tmp_path):
        path = tmp_path / "devices.yaml"
        path.write_text(
            "devices:\n"
            "  d:\n"
            "    base_url: http://h:5000/\n"
            "    profile: {architecture: x86_64}\n"
        )
        assert load(path, {}).base_url == "http://h:5000"

    def test_missing_base_url_rejected(self, tmp_path):
        path = tmp_path / "devices.yaml"
        path.write_text("devices:\n  d:\n    profile: {architecture: x86_64}\n")
        with pytest.raises(HarnessConfigError, match="base_url"):
            load(path, {})

    def test_missing_config_file_rejected(self, tmp_path):
        with pytest.raises(HarnessConfigError, match="Cannot read"):
            load(tmp_path / "absent.yaml", {})


class TestEnvOverridePrecedence:
    def test_env_base_url_wins_over_file(self, config_file):
        target = load(
            config_file,
            {
                "DDA_HARNESS_DEVICE": "jp6-orinagx",
                "DDA_HARNESS_BASE_URL": "http://tunnel:15000",
            },
        )
        assert target.base_url == "http://tunnel:15000"

    def test_env_capabilities_replace_file_list(self, config_file):
        target = load(
            config_file,
            {
                "DDA_HARNESS_DEVICE": "jp6-orinagx",
                "DDA_HARNESS_CAPABILITIES": "vllm, workflows",
            },
        )
        assert target.profile.capabilities == frozenset({"vllm", "workflows"})

    def test_env_architecture_wins_and_is_validated(self, config_file):
        target = load(
            config_file,
            {
                "DDA_HARNESS_DEVICE": "jp6-orinagx",
                "DDA_HARNESS_ARCHITECTURE": "x86_64",
            },
        )
        assert target.profile.architecture == "x86_64"

    def test_env_timeout_wins_over_file_value(self, config_file):
        target = load(
            config_file,
            {
                "DDA_HARNESS_DEVICE": "jp6-orinagx",
                "DDA_HARNESS_VLLM_READY_S": "1200",
            },
        )
        assert target.timeouts.vllm_ready_s == 1200.0

    def test_env_expected_models_replace_file_list(self, config_file):
        target = load(
            config_file,
            {
                "DDA_HARNESS_DEVICE": "jp6-orinagx",
                "DDA_HARNESS_EXPECTED_VISION_MODELS": "only-this-model",
            },
        )
        assert target.expected.vision_models == ("only-this-model",)

    def test_pure_env_target_without_file(self):
        target = load_config(
            config_path=None,
            environ={
                "DDA_HARNESS_DEVICE": "adhoc",
                "DDA_HARNESS_BASE_URL": "http://h:5000",
                "DDA_HARNESS_ARCHITECTURE": "arm64_jp6",
                "DDA_HARNESS_CAPABILITIES": "vllm",
            },
        )
        assert target.name == "adhoc"
        assert target.profile.grants("vllm")


class TestFailClosedValidation:
    def test_unknown_capability_in_file_rejected(self, tmp_path):
        path = tmp_path / "devices.yaml"
        path.write_text(
            "devices:\n"
            "  d:\n"
            "    base_url: http://h:5000\n"
            "    profile:\n"
            "      architecture: arm64_jp6\n"
            "      capabilities: [vllm, warp_drive]\n"
        )
        with pytest.raises(HarnessConfigError, match="warp_drive"):
            load(path, {})

    def test_unknown_capability_from_env_rejected(self, config_file):
        with pytest.raises(HarnessConfigError, match="turbo"):
            load(
                config_file,
                {
                    "DDA_HARNESS_DEVICE": "jp6-orinagx",
                    "DDA_HARNESS_CAPABILITIES": "vllm,turbo",
                },
            )

    def test_unknown_architecture_rejected(self, tmp_path):
        path = tmp_path / "devices.yaml"
        path.write_text(
            "devices:\n"
            "  d:\n"
            "    base_url: http://h:5000\n"
            "    profile: {architecture: riscv}\n"
        )
        with pytest.raises(HarnessConfigError, match="riscv"):
            load(path, {})

    def test_unknown_timeout_key_rejected(self, tmp_path):
        path = tmp_path / "devices.yaml"
        path.write_text(
            "devices:\n"
            "  d:\n"
            "    base_url: http://h:5000\n"
            "    profile: {architecture: x86_64}\n"
            "    timeouts: {warmup_s: 5}\n"
        )
        with pytest.raises(HarnessConfigError, match="warmup_s"):
            load(path, {})

    def test_non_positive_timeout_rejected(self, tmp_path):
        path = tmp_path / "devices.yaml"
        path.write_text(
            "devices:\n"
            "  d:\n"
            "    base_url: http://h:5000\n"
            "    profile: {architecture: x86_64}\n"
            "    timeouts: {generate_s: 0}\n"
        )
        with pytest.raises(HarnessConfigError, match="positive"):
            load(path, {})


class TestTimeoutDefaults:
    def test_design_defaults_applied_when_unset(self, config_file):
        target = load(config_file, {"DDA_HARNESS_DEVICE": "jp5-xavier"})
        assert target.timeouts == Timeouts(
            model_ready_s=300.0,
            vllm_ready_s=900.0,
            generate_s=120.0,
            workflow_output_s=180.0,
            run_budget_s=2400.0,
            continuous_window_s=30.0,
        )

    def test_continuous_window_from_file_and_env(self, tmp_path):
        path = tmp_path / "devices.yaml"
        path.write_text(
            "devices:\n"
            "  d:\n"
            "    base_url: http://h:5000\n"
            "    profile: {architecture: arm64_jp7}\n"
            "    timeouts: {continuous_window_s: 45}\n"
        )
        assert load(path, {}).timeouts.continuous_window_s == 45.0
        env = {"DDA_HARNESS_CONTINUOUS_WINDOW_S": "12"}
        assert load(path, env).timeouts.continuous_window_s == 12.0

    def test_file_partially_overrides_defaults(self, config_file):
        target = load(config_file, {"DDA_HARNESS_DEVICE": "jp6-orinagx"})
        assert target.timeouts.vllm_ready_s == 600.0
        assert target.timeouts.model_ready_s == 300.0  # untouched default


class TestCredentialReferences:
    def test_env_scheme_parsed_without_reading_value(self, config_file):
        # The referenced variable is deliberately NOT set: parsing must not
        # attempt to resolve the value.
        target = load(config_file, {"DDA_HARNESS_DEVICE": "jp6-orinagx"})
        assert target.credentials_ref == CredentialRef("env", "DDA_HARNESS_TOKEN")

    def test_file_scheme_parsed(self):
        ref = CredentialRef.parse("file:~/.dda/jp6-token")
        assert ref.scheme == "file"
        assert ref.locator == "~/.dda/jp6-token"

    def test_unknown_scheme_rejected(self):
        with pytest.raises(HarnessConfigError, match="env:VAR_NAME") as excinfo:
            CredentialRef.parse("vault:secret")
        # A malformed reference is most often a pasted secret: never echoed.
        assert "vault:secret" not in str(excinfo.value)

    def test_pasted_secret_never_echoed_and_context_named(self, config_file):
        with pytest.raises(HarnessConfigError) as excinfo:
            load(
                config_file,
                {
                    "DDA_HARNESS_DEVICE": "jp6-orinagx",
                    "DDA_HARNESS_CREDENTIALS": "admin:hunter2-SECRET",
                },
            )
        message = str(excinfo.value)
        assert "hunter2-SECRET" not in message
        assert "credentials reference for device 'jp6-orinagx'" in message

    def test_bare_value_without_scheme_rejected(self):
        with pytest.raises(HarnessConfigError):
            CredentialRef.parse("just-a-raw-token")

    def test_env_override_wins_over_file_credentials(self, config_file):
        target = load(
            config_file,
            {
                "DDA_HARNESS_DEVICE": "jp6-orinagx",
                "DDA_HARNESS_CREDENTIALS": "file:/run/secrets/token",
            },
        )
        assert target.credentials_ref == CredentialRef("file", "/run/secrets/token")

    def test_omitted_credentials_yield_none(self, config_file):
        target = load(config_file, {"DDA_HARNESS_DEVICE": "jp5-xavier"})
        assert target.credentials_ref is None

    def test_resolve_env_reads_value_at_use_time(self):
        ref = CredentialRef("env", "MY_TOKEN")
        assert ref.resolve(environ={"MY_TOKEN": "s3cret"}) == "s3cret"

    def test_resolve_env_missing_variable_fails(self):
        with pytest.raises(HarnessConfigError, match="MY_TOKEN"):
            CredentialRef("env", "MY_TOKEN").resolve(environ={})

    def test_resolve_file_reads_and_strips(self, tmp_path):
        token_file = tmp_path / "token"
        token_file.write_text("s3cret\n")
        assert CredentialRef("file", str(token_file)).resolve() == "s3cret"

    def test_resolve_missing_file_fails(self, tmp_path):
        with pytest.raises(HarnessConfigError, match="Cannot read"):
            CredentialRef("file", str(tmp_path / "absent")).resolve()

    def test_credential_value_never_in_config_reprs(self, config_file):
        env = {
            "DDA_HARNESS_DEVICE": "jp6-orinagx",
            "DDA_HARNESS_TOKEN": "SUPER-SECRET-VALUE",
        }
        target = load(config_file, env)
        assert "SUPER-SECRET-VALUE" not in repr(target)
        assert "SUPER-SECRET-VALUE" not in repr(target.credentials_ref)
        assert "SUPER-SECRET-VALUE" not in str(target.credentials_ref)


class TestDataclassShapes:
    def test_profile_grants(self):
        profile = DeviceProfile("arm64_jp6", frozenset({"vllm"}))
        assert profile.grants("vllm")
        assert not profile.grants("dlr_models")

    def test_target_is_frozen(self, config_file):
        target = load(config_file, {"DDA_HARNESS_DEVICE": "jp5-xavier"})
        with pytest.raises(dataclasses.FrozenInstanceError):
            target.base_url = "http://elsewhere"


STREAM_YAML = """
devices:
  jp7-thor:
    base_url: http://localhost:5000
    profile:
      architecture: arm64_jp7
      capabilities: [workflows, stream_cameras]
    expected:
      stream_urls:
        - rtsp://192.168.88.237:8554/h264
        - rtmp://192.168.88.237:1935/live/h265
      stream_secure_url: rtsp://192.168.88.237:8554/secure
      stream_credentials: env:DDA_HARNESS_STREAM_SECRET
      stream_failures:
        not_found: rtsp://192.168.88.237:8554/nosuchpath
        tls_verification_failed:
          - rtsps://192.168.88.237:8322/h264
          - rtmps://192.168.88.237:1936/live/h264
      stream_workflow: wf-stream-trigger
      continuous_workflow: wf-stream-continuous
"""


def stream_config(tmp_path, expected_yaml: str):
    """A one-device devices.yaml whose ``expected`` block is ``expected_yaml``
    (already indented under ``expected:``)."""
    path = tmp_path / "devices.yaml"
    path.write_text(
        "devices:\n"
        "  d:\n"
        "    base_url: http://h:5000\n"
        "    profile: {architecture: arm64_jp6, capabilities: [stream_cameras]}\n"
        "    expected:\n" + expected_yaml
    )
    return path


class TestStreamCamerasConfig:
    @pytest.fixture
    def stream_file(self, tmp_path):
        path = tmp_path / "devices.yaml"
        path.write_text(STREAM_YAML)
        return path

    def test_stream_cameras_is_a_known_capability(self, stream_file):
        assert "stream_cameras" in KNOWN_CAPABILITIES
        assert load(stream_file, {}).profile.grants("stream_cameras")
        env = {"DDA_HARNESS_CAPABILITIES": "stream_cameras"}
        assert load(stream_file, env).profile.capabilities == frozenset({"stream_cameras"})

    def test_stream_inputs_parsed_from_file(self, stream_file):
        expected = load(stream_file, {}).expected
        assert expected.stream_urls == (
            "rtsp://192.168.88.237:8554/h264",
            "rtmp://192.168.88.237:1935/live/h265",
        )
        assert expected.stream_secure_url == "rtsp://192.168.88.237:8554/secure"
        assert expected.stream_credentials == CredentialRef("env", "DDA_HARNESS_STREAM_SECRET")
        # A single URL or a list per category, flattened in file order.
        assert expected.stream_failures == (
            ("not_found", "rtsp://192.168.88.237:8554/nosuchpath"),
            ("tls_verification_failed", "rtsps://192.168.88.237:8322/h264"),
            ("tls_verification_failed", "rtmps://192.168.88.237:1936/live/h264"),
        )
        assert expected.stream_workflow == "wf-stream-trigger"
        assert expected.continuous_workflow == "wf-stream-continuous"

    def test_stream_inputs_default_to_unset(self, config_file):
        expected = load(config_file, {"DDA_HARNESS_DEVICE": "jp6-orinagx"}).expected
        assert expected.stream_urls == ()
        assert expected.stream_secure_url is None
        assert expected.stream_credentials is None
        assert expected.stream_failures == ()
        assert expected.stream_workflow is None
        assert expected.continuous_workflow is None
        assert ExpectedComponents().stream_failures == ()

    def test_env_overrides_stream_inputs(self, stream_file):
        expected = load(
            stream_file,
            {
                "DDA_HARNESS_EXPECTED_STREAM_URLS": "rtsp://cam:554/a, rtmps://cam/live/b",
                "DDA_HARNESS_EXPECTED_STREAM_SECURE_URL": "rtsps://cam/secure",
                "DDA_HARNESS_EXPECTED_STREAM_CREDENTIALS": "file:~/.dda/stream",
                "DDA_HARNESS_EXPECTED_STREAM_WORKFLOW": "wf-other",
                "DDA_HARNESS_EXPECTED_CONTINUOUS_WORKFLOW": "wf-cont-other",
            },
        ).expected
        assert expected.stream_urls == ("rtsp://cam:554/a", "rtmps://cam/live/b")
        assert expected.stream_secure_url == "rtsps://cam/secure"
        assert expected.stream_credentials == CredentialRef("file", "~/.dda/stream")
        assert expected.stream_workflow == "wf-other"
        assert expected.continuous_workflow == "wf-cont-other"

    def test_empty_env_value_clears_a_file_input(self, stream_file):
        expected = load(
            stream_file,
            {
                "DDA_HARNESS_EXPECTED_STREAM_URLS": "",
                "DDA_HARNESS_EXPECTED_STREAM_WORKFLOW": "",
                "DDA_HARNESS_EXPECTED_STREAM_CREDENTIALS": "",
            },
        ).expected
        assert expected.stream_urls == ()
        assert expected.stream_workflow is None
        assert expected.stream_credentials is None

    def test_stream_failures_are_file_only(self, stream_file):
        env = {"DDA_HARNESS_EXPECTED_STREAM_FAILURES": "not_found=rtsp://cam/x"}
        with pytest.raises(HarnessConfigError, match="devices.yaml only"):
            load(stream_file, env)

    def test_unknown_failure_category_rejected(self, tmp_path):
        path = stream_config(tmp_path, "      stream_failures: {notfound: rtsp://cam/x}\n")
        with pytest.raises(HarnessConfigError, match="notfound") as excinfo:
            load(path, {})
        assert "not_found" in str(excinfo.value)  # the known vocabulary is listed

    def test_failure_categories_mirror_the_device_vocabulary(self):
        assert {
            "authentication_failed",
            "not_found",
            "unsupported_codec",
            "decoder_unavailable",
            "tls_verification_failed",
            "timeout",
            "network_error",
        } <= KNOWN_STREAM_FAILURE_CATEGORIES

    def test_stream_failures_must_be_a_mapping(self, tmp_path):
        path = stream_config(tmp_path, "      stream_failures: [rtsp://cam/x]\n")
        with pytest.raises(HarnessConfigError, match="mapping"):
            load(path, {})

    def test_url_with_user_info_rejected_without_echo(self, stream_file):
        env = {"DDA_HARNESS_EXPECTED_STREAM_URLS": "rtsp://admin:hunter2@cam:554/a"}
        with pytest.raises(HarnessConfigError, match="user information") as excinfo:
            load(stream_file, env)
        assert "hunter2" not in str(excinfo.value)
        assert "DDA_HARNESS_EXPECTED_STREAM_URLS[0]" in str(excinfo.value)

    def test_url_with_secret_query_parameter_rejected_without_echo(self, tmp_path):
        path = stream_config(
            tmp_path, "      stream_secure_url: rtmp://cam/live/x?user=u&pass=hunter2\n"
        )
        with pytest.raises(HarnessConfigError, match="'pass'") as excinfo:
            load(path, {})
        assert "hunter2" not in str(excinfo.value)

    def test_failure_url_validated_like_the_others(self, tmp_path):
        path = stream_config(
            tmp_path, "      stream_failures: {not_found: [rtsp://u:hunter2@cam/x]}\n"
        )
        with pytest.raises(HarnessConfigError, match=r"stream_failures\.not_found\[0\]") as excinfo:
            load(path, {})
        assert "hunter2" not in str(excinfo.value)

    @pytest.mark.parametrize(
        "url, reason",
        [
            ("http://cam/x", "must be a stream URL"),
            ("RTSP://cam/x", "must be a stream URL"),
            ("rtsp:///x", "has no host"),
            ("rtsp://cam:99999/x", "not a valid URL"),
            ("rtsp://cam/a b", "whitespace"),
        ],
    )
    def test_malformed_stream_urls_rejected(self, tmp_path, url, reason):
        path = stream_config(tmp_path, f"      stream_urls: ['{url}']\n")
        with pytest.raises(HarnessConfigError, match=reason):
            load(path, {})

    def test_pasted_stream_credentials_never_echoed(self, stream_file):
        env = {"DDA_HARNESS_EXPECTED_STREAM_CREDENTIALS": "camuser:hunter2-SECRET"}
        with pytest.raises(HarnessConfigError, match="expected.stream_credentials") as excinfo:
            load(stream_file, env)
        assert "hunter2-SECRET" not in str(excinfo.value)

    def test_stream_credentials_value_never_in_config_reprs(self, stream_file):
        env = {"DDA_HARNESS_STREAM_SECRET": "camuser:SUPER-SECRET-VALUE"}
        target = load(stream_file, env)
        assert "SUPER-SECRET-VALUE" not in repr(target)

    def test_example_file_loads_for_every_device(self):
        example = Path(__file__).resolve().parent.parent / "devices.yaml.example"
        for name in ("jp6-orinagx", "jp5-xavier", "jp7-thor"):
            target = load(example, {"DDA_HARNESS_DEVICE": name})
            assert target.name == name
        thor = load(example, {"DDA_HARNESS_DEVICE": "jp7-thor"})
        assert thor.profile.grants("stream_cameras")
        assert len(thor.expected.stream_urls) == 6
        assert {category for category, _ in thor.expected.stream_failures} == {
            "not_found",
            "unsupported_codec",
            "tls_verification_failed",
        }

    def test_stream_source_type_from_scheme(self):
        assert stream_source_type("rtsp://cam/x") == "RTSP"
        assert stream_source_type("rtsps://cam/x") == "RTSP"
        assert stream_source_type("rtmp://cam/live/x") == "RTMP"
        assert stream_source_type("rtmps://cam/live/x") == "RTMP"


class TestStreamCredentials:
    def test_resolves_username_and_password(self):
        credentials = resolve_stream_credentials(
            CredentialRef("env", "S"), environ={"S": "camuser:pa:ss"}
        )
        assert credentials.username == "camuser"
        assert credentials.password == "pa:ss"  # split at the first colon only

    def test_password_repr_redacted_everywhere(self):
        credentials = resolve_stream_credentials(
            CredentialRef("env", "S"), environ={"S": "camuser:hunter2-SECRET"}
        )
        assert "hunter2-SECRET" not in repr(credentials)
        assert "hunter2-SECRET" not in repr(credentials.password)
        assert "hunter2-SECRET" not in repr(credentials.request_body())

    def test_request_body_json_carries_the_real_password(self):
        credentials = StreamCredentials("camuser", SecretStr("hunter2-SECRET"))
        assert json.loads(json.dumps({"credentials": credentials.request_body()})) == {
            "credentials": {"username": "camuser", "password": "hunter2-SECRET"}
        }

    @pytest.mark.parametrize("value", ["no-colon-SECRET", ":only-password-SECRET", "user:"])
    def test_value_without_both_parts_rejected_without_echo(self, value):
        with pytest.raises(HarnessConfigError, match="username:password") as excinfo:
            resolve_stream_credentials(CredentialRef("env", "S"), environ={"S": value})
        assert "SECRET" not in str(excinfo.value)
        assert "env:S" in str(excinfo.value)  # the reference is named

    def test_unresolvable_reference_names_the_variable(self):
        with pytest.raises(HarnessConfigError, match="S_UNSET"):
            resolve_stream_credentials(CredentialRef("env", "S_UNSET"), environ={})
