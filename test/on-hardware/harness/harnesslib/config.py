"""Harness_Configuration loading and validation for the Edge_Test_Harness.

Merges a ``devices.yaml`` file with ``DDA_HARNESS_*`` environment overrides
into a single validated :class:`DeviceTarget` (Reqs 1.1, 1.2). Validation is
fail-closed: unknown architectures and unknown capability names are rejected
so a typo cannot silently reduce coverage (Req 2.1 support).

Credentials are handled as *references* (``env:VAR`` / ``file:path``) — the
secret value is never read into configuration objects, so reprs, logs, and
the results bundle can never leak it (Req 3.3 support).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Set

import yaml

# Known Device_Profile vocabulary (design: Data Models).
KNOWN_ARCHITECTURES = frozenset({"x86_64", "arm64_jp4", "arm64_jp5", "arm64_jp6"})
KNOWN_CAPABILITIES = frozenset({"vllm", "dlr_models", "onnx_models", "workflows", "auth_enabled"})

# Environment variable names.
ENV_CONFIG = "DDA_HARNESS_CONFIG"
ENV_DEVICE = "DDA_HARNESS_DEVICE"
ENV_BASE_URL = "DDA_HARNESS_BASE_URL"
ENV_ARCHITECTURE = "DDA_HARNESS_ARCHITECTURE"
ENV_CAPABILITIES = "DDA_HARNESS_CAPABILITIES"
ENV_CREDENTIALS = "DDA_HARNESS_CREDENTIALS"

_HARNESS_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = _HARNESS_DIR / "devices.yaml"


class HarnessConfigError(Exception):
    """Raised when the Harness_Configuration is missing, malformed, or invalid."""


@dataclass(frozen=True)
class CredentialRef:
    """A reference to a credential value; never holds the value itself.

    Supported schemes:
      * ``env:VAR_NAME`` — resolve from the process environment at use time.
      * ``file:/path``   — resolve from a file (``~`` expanded) at use time.
    """

    scheme: str
    locator: str

    @classmethod
    def parse(cls, raw: str) -> "CredentialRef":
        scheme, sep, locator = raw.partition(":")
        if not sep or scheme not in ("env", "file") or not locator:
            raise HarnessConfigError(
                f"Invalid credentials reference {raw!r}: expected 'env:VAR_NAME' "
                "or 'file:/path/to/token'"
            )
        return cls(scheme=scheme, locator=locator)

    def resolve(self, environ: Optional[Mapping[str, str]] = None) -> str:
        """Read the credential value. Callers must not log the return value."""
        if self.scheme == "env":
            env = os.environ if environ is None else environ
            value = env.get(self.locator)
            if value is None:
                raise HarnessConfigError(
                    f"Credentials environment variable {self.locator!r} is not set"
                )
            return value
        path = Path(self.locator).expanduser()
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError as err:
            raise HarnessConfigError(
                f"Cannot read credentials file {str(path)!r}: {err.strerror or err}"
            ) from err

    def __str__(self) -> str:
        return f"{self.scheme}:{self.locator}"


@dataclass(frozen=True)
class DeviceProfile:
    """Declared characteristics of a Target_Device used for stage selection."""

    architecture: str
    capabilities: frozenset = frozenset()

    def grants(self, capability: str) -> bool:
        return capability in self.capabilities


@dataclass(frozen=True)
class Timeouts:
    """Per-stage timeout bounds in seconds (design defaults)."""

    model_ready_s: float = 300.0
    vllm_ready_s: float = 900.0
    generate_s: float = 120.0
    workflow_output_s: float = 180.0
    run_budget_s: float = 2400.0


@dataclass(frozen=True)
class ExpectedComponents:
    """Components the Harness_Configuration expects present on the device."""

    vision_models: tuple = ()
    vllm_models: tuple = ()
    workflows: tuple = ()


@dataclass(frozen=True)
class DeviceTarget:
    """One fully-resolved target device the harness runs against."""

    name: str
    base_url: str
    profile: DeviceProfile
    credentials_ref: Optional[CredentialRef] = None
    expected: ExpectedComponents = field(default_factory=ExpectedComponents)
    timeouts: Timeouts = field(default_factory=Timeouts)


def _require_mapping(value, context: str) -> Dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise HarnessConfigError(f"{context} must be a mapping, got {type(value).__name__}")
    return value


def _load_yaml_file(path: Path) -> Dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        raise HarnessConfigError(
            f"Cannot read harness config file {str(path)!r}: {err.strerror or err}"
        ) from err
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise HarnessConfigError(f"Malformed YAML in {str(path)!r}: {err}") from err
    return _require_mapping(data, f"Top level of {str(path)!r}")


def _split_csv(raw: str) -> List[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _validate_architecture(architecture: str, device_name: str) -> str:
    if architecture not in KNOWN_ARCHITECTURES:
        raise HarnessConfigError(
            f"Device {device_name!r}: unknown architecture {architecture!r}; "
            f"known: {', '.join(sorted(KNOWN_ARCHITECTURES))}"
        )
    return architecture


def _validate_capabilities(capabilities, device_name: str) -> frozenset:
    if isinstance(capabilities, str):
        capabilities = _split_csv(capabilities)
    if not isinstance(capabilities, (list, tuple, set, frozenset)):
        raise HarnessConfigError(f"Device {device_name!r}: capabilities must be a list of names")
    caps: Set[str] = set(capabilities)
    unknown = caps - KNOWN_CAPABILITIES
    if unknown:
        # Fail closed: never run with a capability vocabulary we do not know.
        raise HarnessConfigError(
            f"Device {device_name!r}: unknown capability name(s) "
            f"{', '.join(sorted(repr(c) for c in unknown))}; "
            f"known: {', '.join(sorted(KNOWN_CAPABILITIES))}"
        )
    return frozenset(caps)


def _parse_timeouts(raw: Dict, environ: Mapping[str, str], device_name: str) -> Timeouts:
    values = {}
    for f in fields(Timeouts):
        candidate = environ.get(f"DDA_HARNESS_{f.name.upper()}", raw.get(f.name))
        if candidate is None:
            continue
        try:
            number = float(candidate)
        except (TypeError, ValueError):
            raise HarnessConfigError(
                f"Device {device_name!r}: timeout {f.name!r} must be a number, "
                f"got {candidate!r}"
            ) from None
        if number <= 0:
            raise HarnessConfigError(
                f"Device {device_name!r}: timeout {f.name!r} must be positive, " f"got {number}"
            )
        values[f.name] = number
    unknown = set(raw) - {f.name for f in fields(Timeouts)}
    if unknown:
        raise HarnessConfigError(
            f"Device {device_name!r}: unknown timeout key(s) "
            f"{', '.join(sorted(repr(k) for k in unknown))}"
        )
    return Timeouts(**values)


def _parse_expected(raw: Dict, environ: Mapping[str, str], device_name: str) -> ExpectedComponents:
    values = {}
    for f in fields(ExpectedComponents):
        env_override = environ.get(f"DDA_HARNESS_EXPECTED_{f.name.upper()}")
        if env_override is not None:
            values[f.name] = tuple(_split_csv(env_override))
            continue
        candidate = raw.get(f.name)
        if candidate is None:
            continue
        if not isinstance(candidate, (list, tuple)):
            raise HarnessConfigError(f"Device {device_name!r}: expected.{f.name} must be a list")
        values[f.name] = tuple(str(item) for item in candidate)
    unknown = set(raw) - {f.name for f in fields(ExpectedComponents)}
    if unknown:
        raise HarnessConfigError(
            f"Device {device_name!r}: unknown expected key(s) "
            f"{', '.join(sorted(repr(k) for k in unknown))}"
        )
    return ExpectedComponents(**values)


def _select_device_entry(
    devices: Dict, environ: Mapping[str, str], config_path: Optional[Path]
) -> "tuple[str, Dict]":
    name = environ.get(ENV_DEVICE)
    if name:
        if name in devices:
            return name, _require_mapping(devices[name], f"Device entry {name!r}")
        if devices:
            raise HarnessConfigError(
                f"{ENV_DEVICE}={name!r} not found in "
                f"{str(config_path) if config_path else 'configuration'}; "
                f"available: {', '.join(sorted(devices))}"
            )
        # Name given but no file: allow a pure-environment target definition.
        return name, {}
    if len(devices) == 1:
        only = next(iter(devices))
        return only, _require_mapping(devices[only], f"Device entry {only!r}")
    if devices:
        raise HarnessConfigError(
            f"Multiple devices configured ({', '.join(sorted(devices))}); "
            f"select one with {ENV_DEVICE}"
        )
    raise HarnessConfigError(
        f"No target device configured: provide a devices.yaml (or {ENV_CONFIG}) "
        f"and/or set {ENV_DEVICE} with DDA_HARNESS_* overrides"
    )


def load_config(
    config_path: Optional[os.PathLike] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> DeviceTarget:
    """Load, merge, and validate the Harness_Configuration for one target.

    Precedence (highest wins): ``DDA_HARNESS_*`` environment overrides, then
    the selected device's entry in the YAML file, then built-in defaults.

    :param config_path: explicit path to ``devices.yaml``; defaults to
        ``$DDA_HARNESS_CONFIG`` or ``devices.yaml`` next to the harness.
    :param environ: environment mapping (defaults to ``os.environ``);
        injectable for tests.
    """
    env = os.environ if environ is None else environ

    path: Optional[Path]
    if config_path is not None:
        path = Path(config_path)
    elif env.get(ENV_CONFIG):
        path = Path(env[ENV_CONFIG])
    elif DEFAULT_CONFIG_PATH.exists():
        path = DEFAULT_CONFIG_PATH
    else:
        path = None

    file_data = _load_yaml_file(path) if path is not None else {}
    devices = _require_mapping(file_data.get("devices"), "'devices' section")

    name, entry = _select_device_entry(devices, env, path)

    base_url = env.get(ENV_BASE_URL, entry.get("base_url"))
    if not base_url:
        raise HarnessConfigError(
            f"Device {name!r}: no base_url configured (set it in devices.yaml "
            f"or via {ENV_BASE_URL})"
        )
    base_url = str(base_url).rstrip("/")

    profile_raw = _require_mapping(entry.get("profile"), f"Device {name!r} profile")
    architecture = env.get(ENV_ARCHITECTURE, profile_raw.get("architecture"))
    if not architecture:
        raise HarnessConfigError(
            f"Device {name!r}: no architecture configured (set profile.architecture "
            f"or {ENV_ARCHITECTURE})"
        )
    architecture = _validate_architecture(str(architecture), name)

    capabilities_raw = env.get(ENV_CAPABILITIES, profile_raw.get("capabilities", []))
    capabilities = _validate_capabilities(capabilities_raw, name)

    credentials_raw = env.get(ENV_CREDENTIALS, entry.get("credentials"))
    credentials_ref = CredentialRef.parse(str(credentials_raw)) if credentials_raw else None

    expected = _parse_expected(
        _require_mapping(entry.get("expected"), f"Device {name!r} expected"), env, name
    )
    timeouts = _parse_timeouts(
        _require_mapping(entry.get("timeouts"), f"Device {name!r} timeouts"), env, name
    )

    return DeviceTarget(
        name=name,
        base_url=base_url,
        profile=DeviceProfile(architecture=architecture, capabilities=capabilities),
        credentials_ref=credentials_ref,
        expected=expected,
        timeouts=timeouts,
    )
