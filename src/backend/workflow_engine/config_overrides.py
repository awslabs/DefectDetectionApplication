#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
"""Configuration-only switches for registered workflows
(bedrock-inspection-chain).

Two keys of the LocalServer component configuration (every recipe's
``DefaultConfiguration`` declares them as empty maps) let an operator
change a deployed workflow without a code or workflow redeploy:

``WorkflowDisabled``
    ``{"<workflow id>" | "*": "true" | "false"}`` — the trigger runtime
    discards every firing of a disabled workflow before activation.
    ``"true"``/``true`` disables, ``"false"``/``false``/absent enables,
    and ANY other value disables with a WARNING (fail closed: a typo can
    only switch a workflow off, never leave it on unnoticed).

``WorkflowNodeOverrides``
    ``{"<workflow id>" | "*": {"<nodeId>": {"<parameter>": value}}}`` —
    per-node parameter overrides the executor applies to the run's copy of
    the compiled document at run start. Only the parameters of
    :data:`OVERRIDE_ALLOW_LIST` are honoured; every value is coerced to the
    catalog descriptor's type and checked with the vendored validator's
    parameter rules (type, enum, min/max, regex). Anything else (another
    parameter, a trigger binding's parameter, an unknown node or
    parameter, a node type that is not executor-level, an invalid value,
    an unparseable subtree) is IGNORED with a WARNING naming the key and
    the reason; the deployed value stands. So an override can tune, but
    never redirect a message or an upload or change what decides a
    verdict.

The ``<workflow id>`` is the Portal workflow id (``WorkflowRegistration.
workflow_id``, the ``<id>`` of the component ``dda.workflow.<id>``),
version-independent; ``"*"`` matches every workflow, an exact id wins
over ``"*"`` per parameter.

:class:`WorkflowOverridesConfig` is modelled on the MCP configuration
poller (``mcp_edge.config.McpConfig``): one immediate read at ``start()``,
then a daemon thread re-reads every :data:`POLL_INTERVAL_SECONDS`; the
parsed result replaces a lock-guarded snapshot. **Only the poller thread
ever touches IPC** — ``snapshot()``, ``is_disabled()`` and
``apply_overrides()`` are in-memory reads, so a slow or absent nucleus
can never delay a trigger firing or a run start. On a failed read the
last good snapshot stands and one WARNING is logged per minute; until the
first successful read the snapshot is empty (nothing overridden, nothing
disabled), which is also the permanent state wherever the IPC reader is
unavailable (tests, non-Greengrass hosts): both consumers then behave
exactly as before the feature.
"""

import copy
import logging
import threading
import time
from typing import (
    Any, Callable, Dict, FrozenSet, List, Mapping, NamedTuple, Optional,
    Tuple,
)

from workflow_engine.vendor.workflow_core.catalog import get_node_type
from workflow_engine.vendor.workflow_core.catalog.models import (
    PARAM_TYPE_BOOL,
    PARAM_TYPE_ENUM,
    PARAM_TYPE_FLOAT,
    PARAM_TYPE_INT,
)
from workflow_engine.vendor.workflow_core.validator import check_parameter_value

logger = logging.getLogger(__name__)

#: The two component-configuration keys (recipe ``DefaultConfiguration``).
NODE_OVERRIDES_KEY = "WorkflowNodeOverrides"
DISABLED_KEY = "WorkflowDisabled"
#: The workflow-id key that matches every workflow.
ANY_WORKFLOW = "*"
#: Re-poll interval of the daemon thread (seconds).
POLL_INTERVAL_SECONDS = 5.0
#: One read-failure WARNING per this many seconds while reads keep failing.
READ_FAILURE_WARNING_INTERVAL_SECONDS = 60.0
#: One WARNING per ignored override key per this many seconds (the apply
#: path runs once per run; the same misconfiguration must not log per run).
IGNORED_OVERRIDE_WARNING_INTERVAL_SECONDS = 60.0

#: Per node type, the ONLY parameters a configuration override may set.
#: Everything that decides the verdict (prompt, answer_format,
#: expected_count_path, render_prompt, on_error), where a result goes
#: (destination, source, topic, payload_template, payload_format,
#: condition) and every trigger-binding parameter (read once at
#: registration, so an override could never apply consistently) is absent
#: on purpose.
OVERRIDE_ALLOW_LIST: Dict[str, Tuple[str, ...]] = {
    "bedrock_inference": (
        "model", "region", "enabled", "error_verdict", "attempts",
        "attempt_timeout_s", "budget_s", "max_image_dimension", "max_tokens",
        "box_scale",
    ),
    "s3_upload": ("presign_get_expiry_s", "timeout_s", "retries"),
    "mqtt_publish": ("qos", "retain"),
}
_ALLOWED_PARAMETER_NAMES = frozenset(
    name for names in OVERRIDE_ALLOW_LIST.values() for name in names
)



class OverridesSnapshot(NamedTuple):
    """One parsed reading of the two keys. ``node_overrides`` is
    ``{workflow id | "*": {nodeId: {parameter: raw value}}}`` (values are
    coerced and checked at apply time, when the node's type is known);
    ``disabled`` holds the workflow ids (or ``"*"``) switched off."""

    node_overrides: Dict[str, Dict[str, Dict[str, Any]]]
    disabled: FrozenSet[str]


EMPTY_SNAPSHOT = OverridesSnapshot({}, frozenset())


def parse_flag(value: Any) -> Optional[bool]:
    """``True``/``False`` for the understood spellings (``true``/``false``
    as bool or as a case-insensitive string), ``None`` for anything else."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    return None


def parse_disabled(raw: Any) -> Tuple[FrozenSet[str], List[str]]:
    """``WorkflowDisabled`` -> (disabled workflow ids, warnings). An
    unrecognised value disables its workflow (fail closed) and warns; a
    subtree that is not a map is treated as empty and warns."""
    if raw is None:
        return frozenset(), []
    if not isinstance(raw, Mapping):
        return frozenset(), [
            "{0} is not a map of workflow ids (got {1}); treating it as "
            "empty".format(DISABLED_KEY, type(raw).__name__)
        ]
    disabled = set()
    warnings: List[str] = []
    for key, value in raw.items():
        workflow_id = str(key)
        flag = parse_flag(value)
        if flag is None:
            warnings.append(
                "{0}[{1!r}] has the unrecognised value {2!r}; disabling the "
                "workflow (only 'true' and 'false' are understood)".format(
                    DISABLED_KEY, workflow_id, value))
            disabled.add(workflow_id)
        elif flag:
            disabled.add(workflow_id)
    return frozenset(disabled), warnings


def parse_node_overrides(
    raw: Any,
) -> Tuple[Dict[str, Dict[str, Dict[str, Any]]], List[str]]:
    """``WorkflowNodeOverrides`` -> (``{workflow: {node: {param: value}}}``,
    warnings). Non-map subtrees, non-scalar values and parameter names no
    node type allows are dropped here with a warning each; the per-type
    allow-list, type coercion and the descriptor constraints are applied
    by :meth:`WorkflowOverridesConfig.apply_overrides`."""
    if raw is None:
        return {}, []
    if not isinstance(raw, Mapping):
        return {}, [
            "{0} is not a map of workflow ids (got {1}); treating it as "
            "empty".format(NODE_OVERRIDES_KEY, type(raw).__name__)
        ]
    parsed: Dict[str, Dict[str, Dict[str, Any]]] = {}
    warnings: List[str] = []
    for workflow_key, nodes in raw.items():
        if not isinstance(nodes, Mapping):
            warnings.append(
                "{0}[{1!r}] is not a map of node ids (got {2}); ignoring "
                "it".format(NODE_OVERRIDES_KEY, str(workflow_key),
                            type(nodes).__name__))
            continue
        per_workflow: Dict[str, Dict[str, Any]] = {}
        for node_id, parameters in nodes.items():
            if not isinstance(parameters, Mapping):
                warnings.append(
                    "{0}[{1!r}][{2!r}] is not a map of parameters (got {3}); "
                    "ignoring it".format(NODE_OVERRIDES_KEY, str(workflow_key),
                                         str(node_id),
                                         type(parameters).__name__))
                continue
            per_node: Dict[str, Any] = {}
            for name, value in parameters.items():
                label = "{0}[{1!r}][{2!r}][{3!r}]".format(
                    NODE_OVERRIDES_KEY, str(workflow_key), str(node_id),
                    str(name))
                if str(name) not in _ALLOWED_PARAMETER_NAMES:
                    warnings.append(
                        "{0} is not an overridable parameter of any node "
                        "type; ignoring it".format(label))
                    continue
                if isinstance(value, (Mapping, list, tuple)):
                    warnings.append(
                        "{0} must be a scalar value (got {1}); ignoring "
                        "it".format(label, type(value).__name__))
                    continue
                per_node[str(name)] = value
            if per_node:
                per_workflow[str(node_id)] = per_node
        if per_workflow:
            parsed[str(workflow_key)] = per_workflow
    return parsed, warnings



def coerce_parameter_value(descriptor: Any, value: Any) -> Any:
    """Coerce a configuration value (Greengrass merges arrive as strings as
    often as as JSON scalars) to the descriptor's declared type where the
    spelling is unambiguous; anything else is returned unchanged for
    ``check_parameter_value`` to reject. bool: ``true``/``false`` strings;
    int: integer strings and whole floats; float: numeric strings; enum: a
    string spelling of a declared member (so ``"1"`` selects qos ``1``)."""
    kind = descriptor.param_type
    if kind == PARAM_TYPE_BOOL:
        flag = parse_flag(value)
        return value if flag is None else flag
    if kind == PARAM_TYPE_INT:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return value
    if kind == PARAM_TYPE_FLOAT:
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                return value
        return value
    if kind == PARAM_TYPE_ENUM:
        members = (descriptor.constraints or {}).get("values") or []
        if isinstance(value, str):
            stripped = value.strip()
            for member in members:
                if isinstance(member, bool):
                    continue
                if isinstance(member, str) and member == stripped:
                    return member
                if isinstance(member, (int, float)) and str(member) == stripped:
                    return member
        return value
    return value


def node_types_from_graph(graph_document: Any) -> Dict[str, str]:
    """``{nodeId: node type}`` from a ``workflow.json`` graph document (or
    an empty map when it is missing or malformed)."""
    nodes = graph_document.get("nodes") if isinstance(graph_document, Mapping) else None
    types: Dict[str, str] = {}
    for node in nodes or ():
        if isinstance(node, Mapping) and isinstance(node.get("id"), str) \
                and isinstance(node.get("type"), str):
            types[node["id"]] = node["type"]
    return types


def default_configuration_reader() -> Callable[[], Any]:
    """The production reader: one ``DefectDetectionConfig`` (its component
    name discovery is cached per instance), a FRESH ``GetConfiguration``
    per call through ``get_component_config`` (waits up to its 10 s
    ``TIME_OUT``; never the startup cache ``get_local_server_config``).
    Imports are deferred: the IPC client only exists on-device, so
    elsewhere the first call raises and the poller keeps the empty
    snapshot."""
    state: Dict[str, Any] = {}

    def read() -> Any:
        reader = state.get("reader")
        if reader is None:
            from defect_detection_config.defect_detection_config import (
                DefectDetectionConfig,
            )
            from utils.ipc_client import get_ipc_client

            reader = DefectDetectionConfig(get_ipc_client())
            state["reader"] = reader
        return reader.get_component_config(
            reader.get_local_server_component_name())

    return read



_UNREAD = object()


class WorkflowOverridesConfig:
    """Live, lock-guarded view of ``WorkflowNodeOverrides`` and
    ``WorkflowDisabled`` (module docstring). ``read_config`` is injectable
    (``() -> the component configuration mapping``); ``clock`` is a
    monotonic-seconds callable for the rate limits. ``snapshot()`` is the
    only API the trigger and run-start paths use; it never calls the
    reader."""

    def __init__(
        self,
        read_config: Optional[Callable[[], Any]] = None,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._read_config = (
            read_config if read_config is not None
            else default_configuration_reader()
        )
        self._poll_interval = poll_interval
        self._clock = clock
        self._lock = threading.Lock()
        self._snapshot = EMPTY_SNAPSHOT
        # The two subtrees exactly as last read: an unchanged configuration
        # is not re-parsed (and its warnings not re-logged) every poll.
        self._last_raw: Any = _UNREAD
        self._read_failure_warned_at: Optional[float] = None
        self._ignored_warned_at: Dict[str, float] = {}
        self._stop_event = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None

    # ----------------------------------------------------------- reads

    def snapshot(self) -> OverridesSnapshot:
        with self._lock:
            return self._snapshot

    def is_disabled(self, workflow_id: Optional[str]) -> bool:
        """True when ``WorkflowDisabled`` switches this workflow (or every
        workflow, ``"*"``) off; ``None`` (a core built without an id, as
        every pre-feature caller does) is never disabled."""
        if workflow_id is None:
            return False
        disabled = self.snapshot().disabled
        return ANY_WORKFLOW in disabled or str(workflow_id) in disabled

    def overrides_for(self, workflow_id: Optional[str]) -> Dict[str, Dict[str, Any]]:
        """``{nodeId: {parameter: raw value}}`` for one workflow: the
        ``"*"`` entries with the exact id's entries layered on top."""
        node_overrides = self.snapshot().node_overrides
        merged: Dict[str, Dict[str, Any]] = {}
        keys = [ANY_WORKFLOW]
        if workflow_id is not None:
            keys.append(str(workflow_id))
        for key in keys:
            for node_id, parameters in (node_overrides.get(key) or {}).items():
                merged.setdefault(node_id, {}).update(parameters)
        return merged

    def has_overrides(self, workflow_id: Optional[str]) -> bool:
        return bool(self.overrides_for(workflow_id))


    def apply_overrides(
        self,
        document: Any,
        workflow_id: Optional[str],
        node_types_by_id: Optional[Mapping[str, str]] = None,
    ) -> Any:
        """The compiled ``document`` with this workflow's overrides written
        into its ``executorBindings`` parameters — a modified deep copy
        when anything applies, the SAME object (byte-identical) when
        nothing does. ``node_types_by_id`` maps node ids to their catalog
        type (from the registration's graph document); a binding missing
        from it is typed by its executor binding kind, which for every
        allow-listed type is the type id itself. Every ignored key is
        logged as a rate-limited WARNING; one INFO line lists the
        ``(node, parameter)`` pairs applied (values omitted)."""
        merged = self.overrides_for(workflow_id)
        if not merged:
            return document
        bindings = document.get("executorBindings") if isinstance(document, Mapping) else None
        if not isinstance(bindings, list):
            return document
        node_types_by_id = node_types_by_id or {}
        changes: List[Tuple[int, str, Any]] = []
        seen = set()
        for index, entry in enumerate(bindings):
            if not isinstance(entry, Mapping):
                continue
            node_id = entry.get("nodeId")
            overrides = merged.get(node_id)
            if not overrides:
                continue
            seen.add(node_id)
            node_type = node_types_by_id.get(node_id) or entry.get("binding")
            descriptor = get_node_type(node_type) if isinstance(node_type, str) else None
            allowed = OVERRIDE_ALLOW_LIST.get(node_type) if descriptor is not None else None
            if allowed is None:
                self._warn_ignored(
                    "{0} for workflow {1!r}: node {2!r} has type {3!r}, which "
                    "takes no configuration overrides; ignoring its {4} "
                    "override(s)".format(NODE_OVERRIDES_KEY, workflow_id,
                                         node_id, node_type, len(overrides)))
                continue
            if not isinstance(entry.get("parameters"), Mapping):
                self._warn_ignored(
                    "{0} for workflow {1!r}: node {2!r} carries no parameters "
                    "map; ignoring its override(s)".format(
                        NODE_OVERRIDES_KEY, workflow_id, node_id))
                continue
            parameters = {p.name: p for p in descriptor.parameters}
            for name, raw_value in overrides.items():
                label = "{0} for workflow {1!r}: {2}.{3}".format(
                    NODE_OVERRIDES_KEY, workflow_id, node_id, name)
                parameter = parameters.get(name)
                if name not in allowed or parameter is None:
                    self._warn_ignored(
                        "{0} is not an overridable parameter of a {1} node; "
                        "ignoring it".format(label, node_type))
                    continue
                value = coerce_parameter_value(parameter, raw_value)
                violation = check_parameter_value(parameter, value)
                if violation is not None:
                    self._warn_ignored(
                        "{0} = {1!r} rejected ({2}); keeping the deployed "
                        "value".format(label, raw_value, violation.message))
                    continue
                changes.append((index, name, value))
        for node_id in merged:
            if node_id not in seen:
                self._warn_ignored(
                    "{0} for workflow {1!r}: node {2!r} is not an "
                    "executor-level node of the deployed document; ignoring "
                    "its override(s)".format(NODE_OVERRIDES_KEY, workflow_id,
                                             node_id))
        if not changes:
            return document
        result = copy.deepcopy(document)
        for index, name, value in changes:
            result["executorBindings"][index]["parameters"][name] = copy.deepcopy(value)
        logger.info(
            "Workflow %s run applies %d configuration override(s): %s",
            workflow_id, len(changes),
            ", ".join("{0}.{1}".format(bindings[index].get("nodeId"), name)
                      for index, name, _ in changes),
        )
        return result


    # --------------------------------------------------------- polling

    def refresh(self) -> OverridesSnapshot:
        """Read the configuration once (on the CALLING thread — the poller's
        in production) and replace the snapshot; never raises. A read that
        raises, or returns anything but a non-empty map (``get_component_
        config`` turns IPC failures into ``{}``, and a real LocalServer
        configuration is never empty), keeps the last good snapshot."""
        try:
            raw = self._read_config()
            if not isinstance(raw, Mapping) or not raw:
                raise ValueError(
                    "the component configuration read returned {0}".format(
                        "an empty map" if isinstance(raw, Mapping)
                        else type(raw).__name__))
        except Exception as error:  # noqa: BLE001 - last good snapshot stands
            self._warn_read_failure(error)
            return self.snapshot()
        self._read_failure_warned_at = None
        subtrees = (raw.get(NODE_OVERRIDES_KEY), raw.get(DISABLED_KEY))
        with self._lock:
            if self._last_raw is not _UNREAD and subtrees == self._last_raw:
                return self._snapshot
        node_overrides, warnings = parse_node_overrides(subtrees[0])
        disabled, more = parse_disabled(subtrees[1])
        for message in warnings + more:
            logger.warning("%s", message)
        snapshot = OverridesSnapshot(node_overrides, disabled)
        with self._lock:
            previous = self._snapshot
            self._snapshot = snapshot
            self._last_raw = copy.deepcopy(subtrees)
        if snapshot != previous:
            logger.info(
                "Workflow configuration overrides changed: node overrides for "
                "%d workflow key(s), %d disabled key(s)",
                len(node_overrides), len(disabled),
            )
        return snapshot

    def start(self) -> None:
        """Read now and start the re-poll thread. Idempotent."""
        self.refresh()
        with self._lock:
            if self._poll_thread is not None and self._poll_thread.is_alive():
                return
            self._stop_event.clear()
            self._poll_thread = threading.Thread(
                target=self._poll_loop, name="workflow-overrides-poller",
                daemon=True,
            )
            self._poll_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._poll_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self._poll_interval + 1)
        self._poll_thread = None

    def _poll_loop(self) -> None:
        while not self._stop_event.wait(self._poll_interval):
            try:
                self.refresh()
            except Exception:  # pragma: no cover - refresh already guards
                logger.exception(
                    "Unexpected error in the workflow overrides poller")

    # ----------------------------------------------------- rate limits

    def _warn_read_failure(self, error: BaseException) -> None:
        now = self._clock()
        last = self._read_failure_warned_at
        if last is not None and now - last < READ_FAILURE_WARNING_INTERVAL_SECONDS:
            return
        self._read_failure_warned_at = now
        logger.warning(
            "Could not read %s/%s from the component configuration; keeping "
            "the last good values: %s", NODE_OVERRIDES_KEY, DISABLED_KEY, error,
        )

    def _warn_ignored(self, message: str) -> None:
        now = self._clock()
        with self._lock:
            last = self._ignored_warned_at.get(message)
            if last is not None and now - last < IGNORED_OVERRIDE_WARNING_INTERVAL_SECONDS:
                return
            self._ignored_warned_at[message] = now
        logger.warning("%s", message)
