.. _gate_plugin:

====
gate
====

Overview
--------

This GStreamer plugin acts as a strategic control point within a media pipeline, determining whether video frames should be transmitted downstream or discarded. The decision is based on the plugin's current state, which can be set either through its properties or dynamically in real-time via messages from an EventBroker.

Properties
----------

-   **open**: 

    [Boolean (true)]. If set to true the frames will always be passed to downstream elements.  If set to false then it will not allow frames to pass unless the 'numframes' property is greater than 0.

    If open is initially set to FALSE during pipeline creation it will pass the first frame downstream to allow the pipeilne to fully transition to the "Playing" state.

-   **numframes**:

    [Integer (0)]: Specifies the number of frames that will passed downstream.  This property will decrement by 1 for each frame that is passed downstream.  A query of this property will return the number of frames remaining that will be passed downstream.

-   **command**:

    [String (NULL)]: This plugin can be controlled through commands sent from :ref:`Message Broker <message_broker>`.  This property specifies the command this plugin will listen to in order to receive updates to it's state.  The payload schema is specified in :ref:`Payload <gate_payload>`

-   **honor-pts**:

    [Boolean (true)]: Flag that indicates to honor the presentation time or not.  Setting to true will cause the Gate to sleep until the presentation time of the buffer when the buffer is NOT passed downstream.  The gate never sleeps if it is passing the buffer downstream.  Setting to false will incur no sleep in any condition.

    Recommendation: Set to false if your sink element does not care about presentation time (e.g. fakesink) and you are trying to process frames as quickly as possible.  Sinks such as ximagesink and autovideosink care about presentation time

.. warning::

    Setting this value to false when your sink element reacts to presentation time can cause your pipeline to hang indefinitely when transitioning from closed to open.

-   **go-to**
    [String (NULL)]:  When the gate is closed it stops flow through the entire pipeline.  This includes other branches (e.g. after a tee) that this gate plugin isn't associated with.  In order to allow data to continue to flow elsewhere then this path must 'complete' (i.e. GstBuffer reaches the end of the pipeline).  This property specifies the name of the element whose source pad will be pushed to instead of the source pad of the gate plugin when the gate is closed.  The honor-pts value is not used when this property is set.

    Recommendation: Point to the plugin directly before your sink plugin, preferably a fakesink, but not a requirement.  This will push the GstBuffer to the source pad of the element just before the sink, meaning the GstBuffer is in effect forwarding onto the sink.

.. warning::

    Pad negotiation is not respected.  Possible to push the GstBuffer to a plugin that is expecting a different size/format.  Take care when not following recommendation.
    
.. _gate_payload:

Payload
-------

The payload accepted by this plugin is a JSON document outlined below.

.. code-block:: none

    {
        "open": true/false, 
        "num_frames": 0-IntMax
    }

Example
-------

This example creates a simple gstremaer pipeline with a gate between a video source and a sink.  The gate will allow the user control over which frames get rendered.  If the environment is configured to initialize :ref:`Message Broker <message_broker>` then the gate can be controlled through messages sent to 'gate_command' command.

..  tabs::

    ..  group-tab:: gst-launch

        .. code-block:: bash

            # Gate that can be opened and closed with message broker.  Stops flow of pipeline when closed
            gst-launch-1.0 videotestsrc ! gate open=true command=gate_command ! ximagesink

            # Gate that is closed, to prevent a 'heavy' operation (in this case jpegenc) until needed.  Does not block flow of the live preview path.
            gst-launch-1.0 videotestsrc ! tee name=t ! queue ! gate open=false go-to=goto ! jpegenc ! emlcapture buffer-message-id=img name=goto interval=0 ! fakesink t. ! queue ! ximagesink

    ..  group-tab:: EventBroker APIs

        .. literalinclude:: ../../../samples/C++/Gate/gate_eventbroker.cpp
                :language: cpp
                :linenos:

    ..  group-tab:: GStreamer APIs

        .. literalinclude:: ../../../samples/C++/Gate/gate_gstreamer.cpp
                :language: cpp
                :linenos:
