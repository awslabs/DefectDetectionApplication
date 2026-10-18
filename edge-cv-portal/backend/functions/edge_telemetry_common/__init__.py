"""Helpers shared by the Portal's edge-telemetry functions (the workflow debug
mode and the workflow telemetry dashboards; workflow-telemetry-dashboards
design §2.2, §2.3, §10.2, §14.4; workflow-debug-mode design §2).

One live copy in the worktree, created by whichever specification lands first
(decision P1 of the debug mode's task list) and extended, never forked, by the
second:

- ``topics``: the ``dda/telemetry`` and ``dda/debug`` topic convention
  (byte-identical to the device copy ``workflow_engine/telemetry/topics.py``);
- ``topology``: the one ``portal`` / ``cross`` predicate;
- ``session_policies``: the per-purpose session policies for ``cross`` calls;
- ``credentials``: the only builder of ``iot``, ``iot-data``,
  ``greengrassv2``, ``s3`` and ``sts`` clients that reach a use case's account;
- ``devices``: the Device_Record listing per use case;
- ``identity``: certificate thumbprint to thing-name resolution;
- ``shadow_pool``: the bounded thread pool for shadow writes.
"""
