.. _eminfer_marshal:

===============
eminfer_marshal
===============

Overview
--------

The EM Inference Marshalling plugin serves as an integration point between the eminfer plugin provided by the EdgeRuntime SDK and the EdgeML-SDK.  This plugin will live as long as products are using the eminfer plugin in DDA and should be deprecated once eminfer is migrated to EdgeML-SDK.  It's purpose is to take the LFV data stored in the metadata of the GstBuffer set by the eminfer plugin and marshal it to a form that is usable by the EdgeML-SDK by storing the same data as an IPayload on the GstBuffer.  See :ref:`Metadata <gst_metadata>` for more details.

The plugin stores the LFV metadata results into a buffer with ID "lfv_results_meta" and the image overlayed with the anomaly highlighting into a buffer with ID "lfv_results_overlay"

Pad Templates
-------------

sink
====

ANY

src
===

ANY

Properties
----------

-   **workflow-id [REQUIRED]**:

    [String (NULL)]: The id of the workflow associated to this pipeline

-   **temp-cap-path [REQUIRED]**:

    [String (NULL)]: The directory where eminfer saves the inference results.  Should be equivalent to the value set in "capture-folder" on the eminfer plugin.

Metadata
--------

The inference results will be attached to the GstBuffer leaving this plugin as metadata.  The ID of this Metadata is "lfv_results".


Example
-------

.. code-block:: bash

    gst-launch-1.0 .... ! eminfer ... ! eminfer_marshal workflow-id=1234 temp-cap-path=/path/to/intermediate/results ! emlcapture meta=lfv_results_meta:meta
