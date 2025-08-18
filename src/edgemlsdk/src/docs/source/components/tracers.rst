GstTracers
----------

Suite of GstTracers to measure performance of GStreamer pipelines

The output of GstTracers, unless otherwise specified, get routed as a MetricEvent from the :ref:`Event broker <event_broker>`.  See [todo] documentation to see how to configure the event broker.

FPS
^^^

This GstTracer will compute the frames per second sent to the source pad of the specified elements.  Pipeline sink elements will not be evaluated.
It can be enabled by setting `GST_TRACERS="fps"`

Parameters:
interval: Frequency, in milliseconds, the FPS metric will be reported
element: List of the instance name of the GStreamer plugin in which to capture FPS data.  Default is empty which means capture FPS for all plugins.
factory: List of the factory names of the GStramer plugins in which to capture FPS data.  Default is empty which mean capture FPS for all plugins

The plugins that will be evaluated will be the set from the union of element and factory

Element Latency
^^^^^^^^^^^^^^^

This GstTracer will report the latency for each element in the pipeline, excluding the sink and source elements. 
It can be enabled by setting `GST_TRACERS="elementlatency"`
A sample report is shown below.

.. code-block:: json

    {
        "identity3": {
            "count": 10,
            "mean": 20062652.5,
            "variance": 1011586.6499998964
        },
        "identity4": {
            "count": 10,
            "mean": 30063684.699999996,
            "variance": 4440861.209996861
        },
        "identity5": {
            "count": 10,
            "mean": 50064518.1,
            "variance": 1453582.2899999889
        },
        "type": "elementlatency"
    }

The `mean` and `variance` for these elements are reported in nanoseconds and calculated based on the last 1 second of data. `count` represents the number of samples seen over the last 1 second of data.
This report is periodically sent every second through the message broker over the `analytics` message id. The latency reported includes the time it takes for the buffer(s) to move from one element to the next.
