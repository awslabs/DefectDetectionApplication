.. _emlcapture_plugin:

==========
emlcapture
==========

Overview
--------

This plugin will send data in a GstBuffer, and it's metadata, to the message broker which routes it to an endpoint as defined by the :ref:`Message Broker <message_broker>` configuration information.  As a GstBuffer may, or may not, be image data the plugin will not transform/compress/encode images, if image manipulation or preprocessing is desired then upstream elements should handle this.  This plugin will send the data as-is without modification.

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

-   **async**: 

    [Boolean (true)]. Flag indicating to publish messages to the Message Broker asynchronously or synchronously.  Default behavior is asynchronous (true).

-   **subscription-id**:

    [String (NULL)]: Subscrpition id to receive capture commands.  If not specified then plugin will not register to remote commands.  When the plugin receives a command to save the data it will save the NEXT GstBuffer/Metadata that passes through the plugin.

-   **interval**:

    [Integer (-1)]: Value specifying the frequency, in milliseconds, that capture should take place.  To capture every frame set this value to 0 and to capture on demand set this value to less than 0.

-   **buffer-message-id**:

    [String (NULL)]: The id of the message containing the GstBuffer published to the Message Broker.

-   **meta**:

    [String (NULL)]: String that dictates which metadata objects are captured.  Only metadata that implements PayloadMeta can be published, :ref:`Metadata <gst_metadata>` for more information.  This string is a comma delimited list of the names of registered PayloadMeta objects that should be captured along with the id of the message to publish to the Message Broker.  Should take the form: <metadata-id1>:<message-broker-id1>,<metadata-id2>:<message-broker-id2>,...

.. _emlcapture_payload:

Payload
-------

The payload of the signal received by this plugin (set by subscription-id) is not parsed for any information.  Any payload will trigger the capturing of the NEXT image received.


Example
-------

This example creates a simple pipline that will publish an image to message-id "image" once every second OR when it receives a message sent to "trigger" via Message Broker.

..  tabs::

    ..  group-tab:: pipeline definition

        .. code-block:: bash

            videotestsrc ! emlcapture subscription-id=trigger buffer-message-id=image interval=1000 ! ximagesink