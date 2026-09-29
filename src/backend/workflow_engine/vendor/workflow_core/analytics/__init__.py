"""Scene analytics: the shared rules behind the Scene_Analytics_Nodes.

One pure implementation of the Detection Counter, the Object Association
check and the Event Gate (rtsp-rtmp-stream-cameras Requirements 13, 14,
15), so that the LocalServer executor bindings and the Portal's cloud
test sandbox harness produce identical metadata for identical inputs
(Requirements 13.9, 14.6, 15.6).

See :mod:`workflow_core.analytics.scene`.
"""
