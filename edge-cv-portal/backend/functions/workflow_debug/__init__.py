"""Pure logic of the workflow debug mode (workflow-debug-mode design §2):

- ``redact``: the shared redaction rules (a byte-identical copy of the device
  module ``workflow_engine/telemetry/debug/redact.py``; its base rule comes
  from ``workflow_core.stream_url`` on the layer the ingest function carries);
- ``envelope``: the ``dda.debug.v1`` message validation;
- ``logs_writer``: the CloudWatch Logs stream name, group creation, batching
  and the write-time and id stamps;
- ``log_filters``: the server-side filters and the ``next`` termination rule;
- ``log_reads``: the per-container read pacing and the 429 mapping;
- ``console_link``: the CloudWatch console deep link;
- ``views``: the ``x86_64_nvidia`` variant notice;
- ``targets``: the ``all_devices`` target set over the Deployments table;
- ``sessions``: the Portal-side state machine and the all-or-nothing
  creation transaction;
- ``shadow`` and ``media`` (added with the API function): the shadow write
  and the Results_Bucket probes.
"""
