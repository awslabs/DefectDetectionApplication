"""Workflow test sandbox harness (Workflow Manager, task 11.3).

Runs inside the x86_64 Fargate sandbox container (infrastructure in
edge-cv-portal/infrastructure/lib/test-runner-stack.ts, task 11.2). The
harness:

1. Downloads the Compiled Pipeline Document and the selected
   Test_Dataset from portal S3.
2. Stages the dataset locally and resolves the ``{dataset_location}``
   placeholder the simulation compiler leaves in dataset-fed source
   elements (Requirement 12.5).
3. Renders the document into a ``gst-launch``-style string exactly as
   LocalServer does (`" ! "` joins, ``t0. ! queue ! ...`` tee branches,
   ``... ! f0.`` funnel links) and executes it with ``Gst.parse_launch``
   — the same dialect ``GstPipelineManager.run_pipeline`` runs.
4. Executes simulation executor bindings (``recording_*``) as recording
   stubs: what would have been actuated (parameters + triggering
   metadata) is recorded instead of contacting any endpoint
   (Requirement 12.6).
5. Writes per-node results ``{nodeId, status, outputs, stubActivity,
   error}`` and flushes the results document to S3 incrementally after
   every update, so mid-run failures retain the results produced so far
   and identify the failing node (Requirements 12.7, 12.10).

Pure logic (rendering, results assembly, dataset staging plans, binding
stubs) lives in :mod:`harness.renderer`, :mod:`harness.results`,
:mod:`harness.dataset` and :mod:`harness.bindings`, importable without
GStreamer or AWS SDKs so it is unit-testable anywhere. The runtime
(S3 + GStreamer) lives in :mod:`harness.harness`.
"""
