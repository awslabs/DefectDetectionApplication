"""Unit tests for harnesslib.config (Reqs 1.1, 1.2, 2.1).

Covers file+env merge precedence, fail-closed validation of architectures and
capability names, credential reference parsing (values never read into config),
and timeout defaults/overrides.
"""

import dataclasses

import pytest
from harnesslib.config import (
    CredentialRef,
    DeviceProfile,
    DeviceTarget,
    HarnessConfigError,
    Timeouts,
    load_config,
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
        )

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
        with pytest.raises(HarnessConfigError, match="vault:secret"):
            CredentialRef.parse("vault:secret")

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
