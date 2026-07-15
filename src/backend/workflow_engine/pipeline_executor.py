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

"""WorkflowExecutor: runs triggered workflow pipelines (Requirements 9.2, 9.3, 9.7).

Registered through :func:`workflow_engine.executor.set_executor`, so every
run happens on the dedicated daemon thread the hook spawns — never on the
API thread and never on the Pipeline_Configuration path (Requirements
13.4, 13.7). Per run the executor:

1. Loads the WorkflowExecution + WorkflowRegistration rows and the
   registration's ``compiled_pipeline.json``.
2. Renders the launch string (``workflow_engine.rendering`` — the same
   dialect the existing builder produces) and the element-name -> nodeId
   map.
3. Scopes the component's ``plugins/<arch>/`` directory to the run
   (``workflow_engine.gst_plugins``) and executes the string through a
   **fresh** ``GstPipelineManager`` instance — inheriting its watchdog,
   error capture, and emltriton tag parsing, so model inference flows
   through emltriton -> embedded Triton exactly as Pipeline_Configuration
   runs do (Requirements 9.2, 9.3, 13.8). The shared manager instance
   used by ``gst_pipeline_executor`` for Pipeline_Configurations is never
   touched (Requirements 13.1, 13.4).
4. On failure, maps the failing element back to its workflow node via the
   compiled-document tags and records status ``failed`` with
   ``failing_node_id``/``error`` on the execution row — the record the
   ``/workflows/executions/{id}`` status endpoint surfaces
   (Requirement 9.7).
5. On success, records ``completed`` and hands the parsed tag values plus
   the document's ``executorBindings`` to the post-run handler — the hook
   task 12.4 (output bindings) plugs into.

Any exception anywhere in a run is contained: the execution row is marked
failed and nothing propagates (Requirement 13.7).
"""

import json
import logging
import os
import shutil
import tempfile
import time
from typing import Callable, Optional

from workflow_engine import executor as executor_hook
from workflow_engine import python_bridge, rendering
from workflow_engine.output_bindings import BedrockInferenceProcessor
from workflow_engine.discovery import (
    COMPILED_PIPELINE_FILE,
    MANIFEST_FILE,
    STATUS_REGISTERED,
)
from workflow_engine.gst_plugins import workflow_plugin_path
from workflow_engine.models import WorkflowExecution, WorkflowRegistration

logger = logging.getLogger(__name__)

EXECUTION_STATUS_PENDING = "pending"
EXECUTION_STATUS_RUNNING = "running"
EXECUTION_STATUS_COMPLETED = "completed"
EXECUTION_STATUS_FAILED = "failed"

#: Post-run handler signature: (registration, compiled_document,
#: tag_values) -> None. Task 12.4 registers the executor-binding
#: processor (digital output / MQTT / OPC UA) here.
PostRunHandler = Callable[[WorkflowRegistration, dict, dict], None]


class _NullLatencyMetrics:
    """No-op stand-in for the Pipeline_Configuration LatencyMetrics.

    ``GstPipelineManager.parse_msg`` records an inference-received
    timestamp on the latency metrics while parsing emltriton tags;
    workflow runs have no capture-id-keyed latency records, so a no-op
    keeps the inherited tag parsing intact without writing to the
    Pipeline_Configuration latency tables (Requirement 13.4).
    """

    def add_timestamp(self, name):  # noqa: D102 - interface shim
        return time.time()


def _default_pipeline_manager_factory():
    """A fresh GstPipelineManager per run.

    Imported lazily so this module (and its tests) stay importable
    without GStreamer. A separate instance per run guarantees workflow
    execution never shares state with the Pipeline_Configuration
    manager in ``gst_pipeline_executor`` (Requirements 13.1, 13.4).
    """
    from gstreamer.gst_pipeline import GstPipelineManager

    return GstPipelineManager()


class WorkflowExecutor:
    """Executes pending workflow runs dispatched by the executor hook."""

    def __init__(
        self,
        session_factory: Optional[Callable] = None,
        pipeline_manager_factory: Optional[Callable] = None,
        post_run_handler: Optional[PostRunHandler] = None,
        bridged_pipeline_runner: Optional[Callable] = None,
        bedrock_processor: Optional[BedrockInferenceProcessor] = None,
    ) -> None:
        if session_factory is None:
            # Imported lazily so the module is importable without the
            # COMPONENT_WORK_PATH environment the DAO layer requires.
            from dao.sqlite_db.sqlite_db_operations import SessionLocal

            session_factory = SessionLocal
        self._session_factory = session_factory
        self._pipeline_manager_factory = (
            pipeline_manager_factory or _default_pipeline_manager_factory
        )
        self._post_run_handler = post_run_handler
        # Runs launch strings containing Custom_Python_Node bridges
        # (appsink/appsrc pairs pumped through handler subprocesses,
        # Requirement 9.8). Injectable for tests without GStreamer.
        self._bridged_pipeline_runner = (
            bridged_pipeline_runner or python_bridge.run_bridged_pipeline
        )
        # Runs bedrock_inference bindings between the pipeline run and
        # the output bindings: the compiled pipeline captured the two
        # input frames; the processor calls the Bedrock runtime and
        # merges {is_anomalous, confidence} into the tag values the
        # post-run handler gates on. Injectable for tests without boto3.
        self._bedrock_processor = bedrock_processor or BedrockInferenceProcessor()

    def set_post_run_handler(self, handler: Optional[PostRunHandler]) -> None:
        """Register the post-pipeline output-binding processor (task 12.4)."""
        self._post_run_handler = handler

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(self, execution_id: str) -> None:
        """Run one pending execution end to end.

        Runs on the hook's daemon thread. Every failure mode ends with
        the execution row marked ``failed`` (with the failing node when
        identifiable) and nothing raised (Requirements 9.7, 13.7).
        """
        session = self._session_factory()
        work_dir: Optional[str] = None
        try:
            execution = session.get(WorkflowExecution, execution_id)
            if execution is None:
                logger.error(
                    "Workflow execution %s was not found; nothing to run",
                    execution_id,
                )
                return
            if execution.status != EXECUTION_STATUS_PENDING:
                logger.warning(
                    "Workflow execution %s is '%s', not pending; skipping",
                    execution_id,
                    execution.status,
                )
                return

            registration = session.get(
                WorkflowRegistration, execution.registration_id
            )
            failure = self._preflight(registration)
            if failure is not None:
                self._finish_failed(session, execution, error=failure)
                return

            document, load_error = self._load_compiled_document(registration)
            if load_error is not None:
                self._finish_failed(session, execution, error=load_error)
                return

            # Custom_Python_Node bridges (Requirement 9.8): replace each
            # emlpython element with the executor-managed appsink/appsrc
            # pair before rendering; the pair keeps the node's id so
            # failures map back to it.
            bridge_specs = python_bridge.bridge_specs(document)
            if bridge_specs:
                document = python_bridge.rewrite_document(document)

            # Per-run working directory for {work_dir}-rooted artifacts
            # (bedrock_inference frame-capture sinks); resolved into the
            # element args before rendering, exactly like the harness
            # resolves {dataset_location}. Removed after the run.
            work_dir = self._prepare_work_dir(document)

            launch_string = rendering.render_launch_string(document)
            if not launch_string:
                self._finish_failed(
                    session,
                    execution,
                    error="Compiled pipeline document renders an empty "
                    "pipeline (no elements)",
                )
                return
            name_map = rendering.element_name_map(document)

            execution.status = EXECUTION_STATUS_RUNNING
            execution.started_at = int(time.time())
            session.commit()
            logger.info(
                "Workflow execution %s (%s v%s) starting pipeline: %s",
                execution_id,
                registration.workflow_id,
                registration.version,
                launch_string,
            )

            plugin_dir = os.path.join(
                registration.artifact_path, "plugins", registration.arch
            )
            # The manifest names the Plugin_Component install roots that
            # join the plugin scan path and carries the pluginChecksums
            # verified before the registry scan (custom-node-designer
            # Requirements 10.6, 11.4). Best effort: a workflow without
            # a loadable manifest keeps the inline-directory behavior
            # (its registration would be invalid anyway).
            manifest = self._load_manifest(registration)
            try:
                with workflow_plugin_path(
                    plugin_dir,
                    manifest=manifest,
                    artifact_path=registration.artifact_path,
                ):
                    if bridge_specs:
                        tag_values = self._run_bridged(
                            registration, bridge_specs, launch_string
                        )
                    else:
                        manager = self._pipeline_manager_factory()
                        tag_values = manager.run_pipeline(
                            launch_string,
                            latency_metrics=_NullLatencyMetrics(),
                        )
            except Exception as e:  # noqa: BLE001 - contained per 13.7
                # Bridge errors carry the Custom_Python_Node id directly
                # (Requirement 9.8); anything else is mapped from the
                # failing element name (Requirement 9.7).
                failing_node_id = getattr(
                    e, "node_id", None
                ) or rendering.failing_node_id_from_error(name_map, str(e))
                logger.error(
                    "Workflow execution %s failed (node %s): %s",
                    execution_id,
                    failing_node_id or "unidentified",
                    e,
                )
                self._finish_failed(
                    session,
                    execution,
                    error=str(e),
                    failing_node_id=failing_node_id,
                )
                return

            # Bedrock comparison inference: runs BEFORE the run is
            # finalized and before the gating/output bindings evaluate.
            # The parsed {is_anomalous, confidence} fields merge into
            # the tag values so downstream filters/conditionals/outputs
            # see them; a failure (network, credentials, unparseable
            # response) marks THIS run failed with the node identified
            # and touches nothing else (Requirement 13.7).
            if self._bedrock_processor.bindings(document):
                try:
                    tag_values = self._bedrock_processor.process(
                        document, tag_values, work_dir
                    )
                except Exception as e:  # noqa: BLE001 - contained per 13.7
                    failing_node_id = getattr(e, "node_id", None)
                    logger.error(
                        "Workflow execution %s failed in Bedrock inference "
                        "(node %s): %s",
                        execution_id,
                        failing_node_id or "unidentified",
                        e,
                    )
                    self._finish_failed(
                        session,
                        execution,
                        error=str(e),
                        failing_node_id=failing_node_id,
                    )
                    return

            execution.status = EXECUTION_STATUS_COMPLETED
            execution.finished_at = int(time.time())
            session.commit()
            logger.info(
                "Workflow execution %s completed; tags: %s",
                execution_id,
                tag_values,
            )
            self._run_post_run_handler(registration, document, tag_values)
        except Exception:  # noqa: BLE001 - contained per 13.7
            logger.exception(
                "Workflow execution %s failed unexpectedly", execution_id
            )
            self._mark_failed_best_effort(
                execution_id, "Workflow executor failed unexpectedly; see logs"
            )
        finally:
            if work_dir is not None:
                shutil.rmtree(work_dir, ignore_errors=True)
            session.close()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _run_bridged(self, registration, bridge_specs, launch_string) -> dict:
        """Run a launch string containing Custom_Python_Node bridges.

        Builds one subprocess bridge per emlpython element (handler
        paths resolved inside the component's artifacts) and hands the
        rewritten string plus the bridges to the bridged runner —
        ``python_bridge.run_bridged_pipeline`` in production, which
        mirrors GstPipelineManager's watchdog/error/tag patterns while
        pumping frames appsink -> subprocess -> appsrc (Requirement 9.8).
        """
        bridges = python_bridge.build_bridges(
            bridge_specs, registration.artifact_path
        )
        try:
            return self._bridged_pipeline_runner(
                launch_string, bridges, latency_metrics=_NullLatencyMetrics()
            )
        finally:
            for bridge in bridges:
                bridge.stop()

    #: Placeholder the compiler leaves in bedrock_inference capture
    #: paths for the executor to resolve per run.
    _WORK_DIR_TOKEN = "{work_dir}"

    @classmethod
    def _needs_work_dir(cls, document: dict) -> bool:
        """True when the document references the {work_dir} placeholder
        (element args or bedrock_inference capturePaths)."""
        for segment in document.get("segments", []):
            for element in segment.get("elements", []):
                for value in (element.get("args") or {}).values():
                    if isinstance(value, str) and cls._WORK_DIR_TOKEN in value:
                        return True
        for binding in document.get("executorBindings") or []:
            paths = binding.get("capturePaths") or {}
            for value in paths.values():
                if isinstance(value, str) and cls._WORK_DIR_TOKEN in value:
                    return True
        return False

    def _prepare_work_dir(self, document: dict) -> Optional[str]:
        """Create the per-run working directory and resolve {work_dir}
        into the document's element args, or None when unused."""
        if not self._needs_work_dir(document):
            return None
        work_dir = tempfile.mkdtemp(prefix="workflow-run-")
        substitutions = rendering.resolve_placeholder(
            document, "work_dir", work_dir
        )
        logger.info(
            "Resolved {work_dir} -> %s (%d element substitution(s))",
            work_dir,
            substitutions,
        )
        return work_dir

    @staticmethod
    def _preflight(registration: Optional[WorkflowRegistration]) -> Optional[str]:
        """Reason the run cannot start, or None when it can."""
        if registration is None:
            return "Workflow registration no longer exists"
        if registration.status != STATUS_REGISTERED:
            # Invalid registrations are rejected at trigger time too; this
            # covers artifacts invalidated between trigger and dispatch.
            return (
                f"Workflow registration '{registration.id}' is "
                f"'{registration.status}' and cannot be run"
            )
        return None

    @staticmethod
    def _load_manifest(registration: WorkflowRegistration) -> Optional[dict]:
        """The artifact set's manifest.json, or None when unreadable."""
        path = os.path.join(registration.artifact_path, MANIFEST_FILE)
        try:
            with open(path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            return None
        return manifest if isinstance(manifest, dict) else None

    @staticmethod
    def _load_compiled_document(registration: WorkflowRegistration):
        """(document, None) or (None, error message)."""
        path = os.path.join(registration.artifact_path, COMPILED_PIPELINE_FILE)
        try:
            with open(path, "r", encoding="utf-8") as f:
                document = json.load(f)
        except (OSError, ValueError) as e:
            return None, f"Cannot load {COMPILED_PIPELINE_FILE}: {e}"
        if not isinstance(document, dict) or not isinstance(
            document.get("segments"), list
        ):
            return None, (
                f"Malformed {COMPILED_PIPELINE_FILE}: missing 'segments' list"
            )
        return document, None

    @staticmethod
    def _finish_failed(
        session,
        execution: WorkflowExecution,
        error: str,
        failing_node_id: Optional[str] = None,
    ) -> None:
        """Record the failure on the execution row — the record the
        existing /workflows/executions status endpoint reports
        (Requirement 9.7)."""
        execution.status = EXECUTION_STATUS_FAILED
        execution.failing_node_id = failing_node_id
        execution.error = error
        execution.finished_at = int(time.time())
        session.commit()

    def _mark_failed_best_effort(self, execution_id: str, error: str) -> None:
        """Last-resort failure marking with a fresh session (the run's own
        session may be the thing that broke)."""
        try:
            session = self._session_factory()
            try:
                execution = session.get(WorkflowExecution, execution_id)
                if execution is not None and execution.status in (
                    EXECUTION_STATUS_PENDING,
                    EXECUTION_STATUS_RUNNING,
                ):
                    self._finish_failed(session, execution, error=error)
            finally:
                session.close()
        except Exception:  # noqa: BLE001 - truly nothing more to do
            logger.exception(
                "Could not record failure for workflow execution %s",
                execution_id,
            )

    def _run_post_run_handler(
        self, registration: WorkflowRegistration, document: dict, tag_values: dict
    ) -> None:
        """Invoke the output-binding hook (task 12.4), contained."""
        if self._post_run_handler is None:
            return
        try:
            self._post_run_handler(registration, document, tag_values)
        except Exception:  # noqa: BLE001 - contained per 13.7
            logger.exception(
                "Workflow post-run handler failed for %s v%s",
                registration.workflow_id,
                registration.version,
            )


def register_workflow_executor(
    session_factory: Optional[Callable] = None,
    pipeline_manager_factory: Optional[Callable] = None,
    post_run_handler: Optional[PostRunHandler] = None,
) -> WorkflowExecutor:
    """Create a WorkflowExecutor and register it as THE executor hook.

    Called from ``runtime.start_workflow_engine`` so triggered runs stop
    staying pending once the engine is up.
    """
    instance = WorkflowExecutor(
        session_factory=session_factory,
        pipeline_manager_factory=pipeline_manager_factory,
        post_run_handler=post_run_handler,
    )
    executor_hook.set_executor(instance.execute)
    logger.info("WorkflowExecutor registered as the workflow executor")
    return instance
