"""Scene analytics: the shared rules behind the Scene_Analytics_Nodes.

One pure implementation of the Detection Counter, the Object Association
check and the Event Gate (rtsp-rtmp-stream-cameras Requirements 13, 14,
15), so that the LocalServer executor bindings and the Portal's cloud
test sandbox harness produce identical metadata for identical inputs
(Requirements 13.9, 14.6, 15.6).

See :mod:`workflow_core.analytics.scene`.

The Layout Check rules live beside them (car-assembly-check Requirements
4.1, 5.2, 5.3): the ``expected_regions`` parser the validator (V14) also
uses, the mask-against-region IoU on run-length intervals, the car
outline rule and the per-class verdict, shared the same way by the device
binding and the sandbox.

See :mod:`workflow_core.analytics.layout`.
"""
