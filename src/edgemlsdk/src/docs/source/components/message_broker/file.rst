.. _file_protocol_client:

====================
File Protocol Client
====================

Overview
--------

This protocol client saves data to disk.  Each publish will create a new file unless a message is published with the same filename in which case it will be overwritten.

Message Broker Integration
--------------------------

Target Parameters
^^^^^^^^^^^^^^^^^

-   **protocol**

    .. code-block:: json

        "protocol": "file"

-   **file_options**

    No arguments needed

    .. code-block:: json

        "file_options": {}

-   **subscriptions**

    Not supported.


File Message Options
^^^^^^^^^^^^^^^^^^^^

-   **directory**

    The directory where the file will be saved.  If the directory does not exist then it will be created.

-   **filename**

    The name of the file.  The following macros are supported

    -   "${id}":  Expands to the ID of the payload
    -   "${c_id}": Expands to the Correlation-ID of the payload
    -   "${timestamp}": Expands to the timestamp of the payload
    -   "${count}": Expands to the number of times a specific message-id was published.  Starts with 0

Message Broker Config Sample
^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: json

    {
        "targets": [
            {
                "protocol": "file",
                "name": "test",
                "file_options": {}
            }
        ],
        "pipes": [
            {
                "message_id": "test_message",
                "destinations": [
                    {
                        "target_name": "test",
                        "file_message_options": {
                            "directory": "./my_directory",
                            "filename": "${timestamp}_test_file-${id}-${c_id}.txt"
                        }
                    }
                ]
            }
        ]
    }

Direct Usage
------------

Following section demonstrates how to use the File Protocol Client directly and not through Message Broker

Sample
------

..  tabs::

    ..  group-tab:: C++

        .. note:: link against libpanorama.core.so

        .. literalinclude:: ../../../samples/C++/MessageBroker/file.cpp
            :language: cpp
            :linenos:

        Output from this sample should produce file ./my-test-directory/my-file-123456 with contents 'hello world' in the file.

    ..  group-tab:: Python

        .. literalinclude:: ../../../samples/Python/MessageBroker/file_protocol_client_sample.py
            :language: python
            :linenos:

API
---

..  tabs::

    ..  group-tab:: C++

        **Interfaces**

        .. doxygenstruct:: Panorama::IFileProtocolMessage
            :members:

        **Factory Methods**

        .. doxygenfunction:: Panorama::MessageBroker::FileProtocolClient(IProtocolClient** ppObj)
        .. doxygenfunction:: Panorama::MessageBroker::FileProtocolMessage(IFileProtocolMessage** ppObj, IPayload* payload, const char* directory, const char* filename)
        .. doxygenfunction:: Panorama::MessageBroker::FileProtocolFactory(IProtocolFactory** ppObj)

    ..  group-tab:: Python

        .. autoclass:: panorama.messagebroker.FileProtocolClient
                :members:
                :undoc-members: