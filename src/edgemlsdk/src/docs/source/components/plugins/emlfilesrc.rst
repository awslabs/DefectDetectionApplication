.. _emlfilesrc_plugin:

==========
emlfilesrc
==========

Overview
--------

Source plugin that will load data from a file and push it downstream when signalled from the Message Broker.

Pad Templates
-------------

src
===

ANY

Properties
----------

-   **subscription-id**:

    [String (NULL)]: Subscription id to receive commands to load a file and push downstream.  Not specifying this plugin will not cause an error, but there will be no way to signal it to load a file.

.. warning::

    This is a source plugin that doesn't immediately push a buffer.  The implication of this means that the pipelne won't fully tranisition to PLAYING until the first message is sent.


Payload
-------

The payload accepted by this plugin is a JSON document outlined below.

.. code-block:: none

    {
        "file-path": "<string> (required)", 
        "correlation-id": "<string> (optional)"
    }

If the correlation-id is specified, it will be attached to the GstBuffer.  See :ref:`Metadata <gst_metadata>` for more information.

Example
-------

This example creates a simple pipline that will publish an image to message-id "image" once every second OR when it receives a message sent to "trigger" via Message Broker.

..  tabs::

    ..  group-tab:: pipeline definition

        .. code-block:: bash

            emlfilesrc subscription-id=trigger ! ximagesink

    ..  group-tab:: C++

            TODO
            https://taskei.amazon.dev/tasks/V1221096838

    ..  group-tab:: Python

            TODO
            https://taskei.amazon.dev/tasks/V1221096838
