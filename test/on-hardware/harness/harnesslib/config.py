"""Harness_Configuration loading and validation for the Edge_Test_Harness.

Merges a ``devices.yaml`` file with ``DDA_HARNESS_*`` environment overrides
into a single validated :class:`DeviceTarget` (Reqs 1.1, 1.2). Validation is
fail-closed: unknown architectures and unknown capability names are rejected
so a typo cannot silently reduce coverage (Req 2.1 support).

Credentials are handled as *references* (``env:VAR`` / ``file:path``) — the
secret value is never read into configuration objects, so reprs, logs, and
the results bundle can never leak it (Req 3.3 support). That holds for the
stream camera stage's ``expected.stream_credentials`` too, and configured
stream URLs are rejected when they embed a credential.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple
from urllib.parse import parse_qsl, urlsplit

import yaml

# Known Device_Profile vocabulary (design: Data Models).
KNOWN_ARCHITECTURES = frozenset(
    {"x86_64", "arm64_cpu", "arm64_jp5", "arm64_jp6", "arm64_jp7"})
KNOWN_CAPABILITIES = frozenset(
    {"vllm", "dlr_models", "onnx_models", "workflows", "auth_enabled", "stream_cameras"}
)

#: Stream URL schemes and the Image_Source type each one maps to (device:
#: ``workflow_core.stream_url.SCHEMES_BY_SOURCE_TYPE``). The stream camera
#: stage infers an Image_Source's type from its URL's scheme.
STREAM_URL_SCHEMES: Dict[str, str] = {
    "rtsp": "RTSP",
    "rtsps": "RTSP",
    "rtmp": "RTMP",
    "rtmps": "RTMP",
}

#: Connection-test failure categories the device reports (device:
#: ``stream_ingest.health.ALL_CATEGORIES``). ``expected.stream_failures`` keys
#: are checked against it fail-closed, so a typo'd category is a
#: configuration error instead of a confusing mismatch on the device.
KNOWN_STREAM_FAILURE_CATEGORIES = frozenset(
    {
        "authentication_failed",
        "decoder_unavailable",
        "hardware_decoder_failed",
        "network_error",
        "not_found",
        "server_error",
        "session_limit",
        "stall",
        "timeout",
        "tls_verification_failed",
        "unsupported_codec",
        "worker_exit",
    }
)

#: Query parameter names that carry secrets (device:
#: ``workflow_core.stream_url.SECRET_QUERY_PARAMETERS``). A configured stream
#: URL carrying one is rejected, so no secret sits in the configuration.
_SECRET_QUERY_PARAMETERS = frozenset(
    {
        "api_key",
        "apikey",
        "auth",
        "key",
        "pass",
        "passwd",
        "password",
        "pwd",
        "secret",
        "sig",
        "signature",
        "stream_key",
        "streamkey",
        "token",
    }
)

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
    def parse(cls, raw: str, context: str = "credentials reference") -> "CredentialRef":
        """Parse a reference; ``context`` names the key in the error.

        The rejected value is never echoed: a malformed reference is most
        often the secret itself pasted in place of a reference (for
        example ``user:pass``), and the error becomes a skip reason in the
        terminal output and the JUnit XML.
        """
        scheme, sep, locator = raw.partition(":")
        if not sep or scheme not in ("env", "file") or not locator:
            raise HarnessConfigError(
                f"Invalid {context}: expected 'env:VAR_NAME' or "
                "'file:/path/to/token', a reference and never the value itself "
                "(the configured value is not shown because it may be a secret)"
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


class SecretStr(str):
    """A resolved secret. It is the ``str`` it holds for JSON encoding and
    comparison, but its ``repr`` is redacted, so the value never shows in a
    repr, in pytest's assertion introspection, or in ``--showlocals``
    output (for example a request body dict in a failing frame)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "'<redacted>'"


@dataclass(frozen=True)
class StreamCredentials:
    """Stream_Credentials resolved from ``expected.stream_credentials``.
    Only the username shows in the repr."""

    username: str
    password: SecretStr = field(repr=False)

    def request_body(self) -> Dict[str, str]:
        """The write-only ``credentials`` object of an Image_Source request."""
        return {"username": self.username, "password": self.password}


def resolve_stream_credentials(
    ref: CredentialRef, environ: Optional[Mapping[str, str]] = None
) -> StreamCredentials:
    """Resolve ``expected.stream_credentials`` to ``username:password``.

    The value is wrapped as soon as it is read, and no error names it; only
    the reference appears in messages. Callers must not log the password.
    """
    raw = SecretStr(ref.resolve(environ))
    username, sep, password = raw.partition(":")
    password = SecretStr(password)
    if not sep or not username or not password:
        raise HarnessConfigError(
            f"expected.stream_credentials ({ref}) must resolve to "
            "'username:password' with both parts non-empty (the resolved "
            "value is not shown)"
        )
    return StreamCredentials(username=username, password=password)


def stream_source_type(url: str) -> str:
    """The Image_Source type (``RTSP`` or ``RTMP``) of a validated stream
    URL, from its scheme."""
    return STREAM_URL_SCHEMES[url.partition("://")[0]]


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
    #: How long the stream stage samples a continuous workflow's counters.
    continuous_window_s: float = 30.0


@dataclass(frozen=True)
class ExpectedComponents:
    """Components the Harness_Configuration expects present on the device,
    plus the stream camera stage's inputs. Every stream input is optional:
    a check whose input is absent skips with a reason naming the key."""

    vision_models: tuple = ()
    vllm_models: tuple = ()
    workflows: tuple = ()
    #: Credential-free stream URLs that must connect; the type comes from
    #: the scheme (rtsp/rtsps -> RTSP, rtmp/rtmps -> RTMP).
    stream_urls: tuple = ()
    #: A stream URL that needs credentials, and the reference they resolve
    #: from (``username:password``).
    stream_secure_url: Optional[str] = None
    stream_credentials: Optional[CredentialRef] = None
    #: ``(category, url)`` pairs: each URL's connection test must fail with
    #: exactly that category. File-only (a mapping in devices.yaml).
    stream_failures: tuple = ()
    #: workflowIds of installed stream workflows: an on_trigger one and a
    #: continuous one.
    stream_workflow: Optional[str] = None
    continuous_workflow: Optional[str] = None


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


#: ``expected.*`` keys holding lists of component names (env: comma-separated).
_EXPECTED_NAME_LISTS = ("vision_models", "vllm_models", "workflows")

#: ``expected.*`` keys holding one workflowId each.
_EXPECTED_WORKFLOW_IDS = ("stream_workflow", "continuous_workflow")


def _expected_env_name(key: str) -> str:
    return f"DDA_HARNESS_EXPECTED_{key.upper()}"


def _validate_stream_url(value: Any, context: str, device_name: str) -> str:
    """A configured stream URL, validated without ever echoing it, since it
    may carry a credential: an rtsp/rtsps/rtmp/rtmps scheme, a host, and no
    user information or secret query parameter (credentials belong in
    ``expected.stream_credentials``). Returns it stripped."""
    prefix = f"Device {device_name!r}: {context}"
    if not isinstance(value, str) or not value.strip():
        raise HarnessConfigError(f"{prefix} must be a non-empty stream URL")
    url = value.strip()
    scheme, sep, _ = url.partition("://")
    if not sep or scheme not in STREAM_URL_SCHEMES:
        schemes = ", ".join(f"{name}://" for name in sorted(STREAM_URL_SCHEMES))
        raise HarnessConfigError(f"{prefix} must be a stream URL starting with one of {schemes}")
    if any(character.isspace() for character in url):
        raise HarnessConfigError(f"{prefix} must not contain whitespace")
    try:
        parts = urlsplit(url)
        _ = parts.port  # raises ValueError on a malformed port
    except ValueError:
        raise HarnessConfigError(f"{prefix} is not a valid URL (the URL is not shown)") from None
    if "@" in parts.netloc:
        raise HarnessConfigError(
            f"{prefix} embeds user information (user:password@); stream "
            "credentials go in expected.stream_credentials, never in a URL "
            "(the URL is not shown)"
        )
    if not parts.hostname:
        raise HarnessConfigError(f"{prefix} has no host")
    for name, _ in parse_qsl(parts.query, keep_blank_values=True):
        if name.lower() in _SECRET_QUERY_PARAMETERS:
            raise HarnessConfigError(
                f"{prefix} carries the secret query parameter {name!r}; stream "
                "credentials go in expected.stream_credentials (the URL is not shown)"
            )
    return url


def _optional_scalar(raw: Dict, environ: Mapping[str, str], key: str, device_name: str):
    """An optional single-valued ``expected.<key>``: the environment wins
    over the file, and an empty value (either source) means unset."""
    value = environ.get(_expected_env_name(key))
    if value is None:
        value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, (dict, list, tuple)):
        raise HarnessConfigError(f"Device {device_name!r}: expected.{key} must be a single value")
    text = str(value).strip()
    return text or None


def _parse_stream_urls(raw: Dict, environ: Mapping[str, str], device_name: str) -> tuple:
    env_name = _expected_env_name("stream_urls")
    env_override = environ.get(env_name)
    if env_override is not None:
        urls, context = _split_csv(env_override), env_name
    else:
        candidate = raw.get("stream_urls")
        if candidate is None:
            return ()
        if not isinstance(candidate, (list, tuple)):
            raise HarnessConfigError(f"Device {device_name!r}: expected.stream_urls must be a list")
        urls, context = list(candidate), "expected.stream_urls"
    return tuple(
        _validate_stream_url(url, f"{context}[{index}]", device_name)
        for index, url in enumerate(urls)
    )


def _parse_stream_failures(raw: Dict, environ: Mapping[str, str], device_name: str) -> tuple:
    """``expected.stream_failures`` — a mapping of failure category to one
    URL or a list of URLs — as ``(category, url)`` pairs in file order."""
    env_name = _expected_env_name("stream_failures")
    if env_name in environ:
        raise HarnessConfigError(
            f"Device {device_name!r}: {env_name} is not supported; "
            "expected.stream_failures is a mapping and is configured in "
            "devices.yaml only"
        )
    candidate = raw.get("stream_failures")
    if candidate is None:
        return ()
    if not isinstance(candidate, dict):
        raise HarnessConfigError(
            f"Device {device_name!r}: expected.stream_failures must be a mapping "
            "of connection-test failure category to a URL or a list of URLs"
        )
    pairs: List[Tuple[str, str]] = []
    for category, urls in candidate.items():
        category = str(category)
        if category not in KNOWN_STREAM_FAILURE_CATEGORIES:
            # Fail closed, as for capability names.
            raise HarnessConfigError(
                f"Device {device_name!r}: unknown stream failure category "
                f"{category!r} in expected.stream_failures; known: "
                f"{', '.join(sorted(KNOWN_STREAM_FAILURE_CATEGORIES))}"
            )
        if isinstance(urls, str):
            urls = [urls]
        if not isinstance(urls, (list, tuple)) or not urls:
            raise HarnessConfigError(
                f"Device {device_name!r}: expected.stream_failures.{category} "
                "must be a URL or a non-empty list of URLs"
            )
        for index, url in enumerate(urls):
            context = f"expected.stream_failures.{category}[{index}]"
            pairs.append((category, _validate_stream_url(url, context, device_name)))
    return tuple(pairs)


def _parse_expected(raw: Dict, environ: Mapping[str, str], device_name: str) -> ExpectedComponents:
    values: Dict[str, Any] = {}
    for key in _EXPECTED_NAME_LISTS:
        env_override = environ.get(_expected_env_name(key))
        if env_override is not None:
            values[key] = tuple(_split_csv(env_override))
            continue
        candidate = raw.get(key)
        if candidate is None:
            continue
        if not isinstance(candidate, (list, tuple)):
            raise HarnessConfigError(f"Device {device_name!r}: expected.{key} must be a list")
        values[key] = tuple(str(item) for item in candidate)

    values["stream_urls"] = _parse_stream_urls(raw, environ, device_name)
    secure_url = _optional_scalar(raw, environ, "stream_secure_url", device_name)
    if secure_url is not None:
        values["stream_secure_url"] = _validate_stream_url(
            secure_url, "expected.stream_secure_url", device_name
        )
    credentials = _optional_scalar(raw, environ, "stream_credentials", device_name)
    if credentials is not None:
        values["stream_credentials"] = CredentialRef.parse(
            credentials,
            context=f"expected.stream_credentials reference for device {device_name!r}",
        )
    values["stream_failures"] = _parse_stream_failures(raw, environ, device_name)
    for key in _EXPECTED_WORKFLOW_IDS:
        values[key] = _optional_scalar(raw, environ, key, device_name)

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
    credentials_ref = (
        CredentialRef.parse(
            str(credentials_raw), context=f"credentials reference for device {name!r}"
        )
        if credentials_raw
        else None
    )

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
