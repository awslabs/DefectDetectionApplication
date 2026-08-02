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

"""LocalServer workflow engine (Workflow Manager feature).

Additive subsystem that discovers, registers, and executes workflows
delivered by Workflow_Components (Greengrass). It never modifies the
existing Pipeline_Configuration path under ``gstreamer/`` (Requirement 13).

Package layout:

- ``vendor/workflow_core``: vendored copy of the shared workflow_core
  package (catalog, serializer, validator, compiler). See
  ``vendor/README.md`` for how to re-vendor.
- ``models.py``: SQLAlchemy models for the additive
  ``workflow_registrations`` and ``workflow_executions`` tables.
- ``environment.py``: device architecture / LocalServer version probes.
- ``discovery.py``: artifact-set scanning and validation (pure functions).
- ``watcher.py``: WorkflowWatcher — startup scan + inotify/poll watch of
  ``/aws_dda/workflows/`` that registers artifact sets (Requirement 9.1).
- ``executor.py``: executor hook the trigger endpoint dispatches through;
  the WorkflowExecutor registers itself here.
- ``rendering.py``: launch-string rendering of the Compiled Pipeline
  Document and element-name -> nodeId failure mapping (pure functions).
- ``camera_binding.py``: pure device-side Camera_Binding resolution —
  ``resolve_bindings`` substitutes bound Camera_Source parameters into a
  compiled document's ``bindingPoints`` slots (camera-registry-sync).
- ``camera_binding_store.py``: CameraBindingStore — cached reads of the
  ``dda-camera-bindings`` named shadow feeding the watcher's binding
  resolution, refreshed on shadow delta (camera-registry-sync).
- ``gst_plugins.py``: per-run scoping of the component's
  ``plugins/<arch>/`` directory (GST_PLUGIN_PATH prepend + registry scan).
- ``pipeline_executor.py``: WorkflowExecutor — executes triggered runs
  through the existing GstPipelineManager (Requirements 9.2, 9.3, 9.7).
- ``python_bridge.py``: Custom_Python_Node bridge — emlpython elements
  rewritten to executor-managed appsink/appsrc pairs pumping frames
  through a limited handler subprocess over a framed stdin/stdout
  protocol (Requirement 9.8).
- ``output_bindings.py``: post-run processing of executorBindings —
  digital output / MQTT publish / OPC UA write (Requirements 9.4-9.6);
  also hosts the pre-output Bedrock and LLM inference processors.
- ``llm_inference.py``: strict Prompt_Template rendering for the
  ``llm_inference`` executor binding (vllm-triton-inference).
- ``api.py``: FastAPI router with the new ``/workflows/registrations``
  and ``/workflows/executions`` endpoints.
- ``runtime.py``: process-wide watcher/executor singletons + startup
  entry point.
"""
