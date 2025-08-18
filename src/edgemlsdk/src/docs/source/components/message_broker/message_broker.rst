.. |br| raw:: html

    <br />

.. _message_broker:

==============
Message Broker
==============

Overview
--------

The Message Broker is a centralized messaging and command relay designed to abstract away the complexities of message destinations and command origins. It serves as an intermediary, ensuring seamless communication between various application components without them needing to be explicitly aware of each other's locations or protocols.

**Core Features & Advantages:**

-   **Endpoint Ignorance for Senders**: 

    Application components can transmit messages without the necessity to specify, or even be aware of, their final destination. This means a message can be dispatched without determining if its endpoint is MQTT, S3, disk, SQL Database, or any other destination. The Event Broker manages these specifics.

-   **Source Agnosticism for Receivers**: 
    
    Components designed to receive commands can do so without knowledge of the command's origin. They simply await the command's receipt, with the Message Broker managing the delivery.

-   **Flexible Configuration of Message Routes**: 

    Developers are provided the autonomy to determine and adjust the pathways for message flow through a dedicated configuration file. This convenience means that altering communication patterns does not require coding changes or application recompilation.

-   **Single-Call Multicast**: 

    Developers can expand the reach of their messages by using the multicast feature, which enables the broadcasting of a single message to multiple destinations simultaneously.

-   **Custom Protocol Injection**: 

    Recognizing the diverse and evolving needs of applications, the Message Broker allows developers to introduce their proprietary protocols for message routing. This ensures that the system remains adaptable to unique or specialized routing requirements.

In essence, the Message Broker offers a streamlined and adaptable messaging platform that abstracts the complexities of communication, allowing both application components and developers to focus on their core functions while enjoying flexible and efficient messaging capabilities.

Protocol Clients
----------------

Protocol clients form the foundational elements of the Message Broker. They are tailored to specific technologies and are designed to facilitate message transmission to particular endpoints, such as MQTT, S3, or SQL-Lite. A protocol client operates on a Publish/Subscribe model, although not every implementation may support both functions. In cases where an operation is not supported, the implementation is expected to yield a "Not Implemented" error for the respective method.

The protocol clients bring along 2 additional concepts

-   **Protocol Subscription**

    This is an entity that holds protocol-specific details required to subscribe to messages. Its complexity varies, ranging from something as straightforward as a string (like an MQTT topic) to more complex forms. Each type of protocol client should come with its own implementation of a Protocol Subscription. 

-   **Protocol Message**

    Every protocol possesses a distinct set of options that can be adjusted when dispatching a message. For instance, with MQTT, you have the ability to designate the topic for publication, set the Quality of Service (QoS), among various other settings. Conversely, for S3, options such as the bucket and key where the message will be stored can be specified. These objects encapsulate the specific parameters needed to publish a message for a given protocol, and every protocol client should provide its own implementation of a Protocol Message.

Available Protocols
^^^^^^^^^^^^^^^^^^^

The SDK comes with several protocol client implementations.  If the available protocol clients do not meed your need you also have the option of creating your own.  See :ref:`Custom Protocols <custom_protocol_client>` for further information


.. toctree::
   :maxdepth: 1

   mqtt
   s3
   file

.. _custom_protocol_client:

Custom Protocols
^^^^^^^^^^^^^^^^

TODO

.. _message_broker_config:

Configuration
-------------

The Message Broker uses a JSON document for configuration, which is organized into the concepts of 'targets' and 'pipes'. This structured configuration lays out the design of the messaging network, determining how messages flow through the system.

-   **Targets**:

    A target represents a potential endpoint where a message can be delivered. Each target is defined by:

    -   **protocol**: 
    
        Describes the communication method used. For example, it could be "mqtt", "s3", etc.
    
    -   **name**: 
        
        A unique identifier for the target. This is used to reference the target in other parts of the configuration.

    -   **\*_options**: 

        A nested JSON object containing parameters to initialize the specific protocol. For instance, for MQTT, you would have 'mqtt_options' with fields like endpoint and region.

    -   **\*_subscription**:

        Array of subscriptions.  Only protocols that have the subscription functionality will react to this.  This will define the subscription id along with any necessary parameters need to establish that subscription. 

    .. code-block:: json

        "targets": [
            {
                "protocol": "xxxx",
                "name": "unique-protocol-name",
                "xxxx_options": {
                    "parameter1": "...",
                    "parameter2": "..."
                },
                "xxxx_subscriptions": [
                    {
                        "subscription-id": "unique-to-this-array",
                        "parameter1": "...",
                        "parameter2": "..."
                    }
                ]
            }
        ]

-   **Pipes**:

    Pipes define which targets will receive a specific published message.  Pipes flow from the application to the endpoint. Each pipe is defined by:

    -   **message_id**: 
    
        The type of message that is published by a component
    
    -   **destinations**: 
    
        The list of targets to route the message to.  Each destination contains

        -   **target_name**: The name of the target to route too
        -   **\*_message_options**: The options used to create the message broker specific message.  Should be prefixed with the value specified in 'protocol' in your target.

    .. code-block:: json

        "pipes": [
            {
                "message_id": "some_message_id",
                "destinations": [
                    {
                        "target_name": "xxxx",
                        "xxxx_message_options": {
                            "parameter1": "...",
                            "parameter2": "..."
                        }
                    }
                ]
            }
        ]

.. _message_broker_create:

Creating the Message Broker
---------------------------

When creating the message broker you will optionally be able to provide the configuration for the message broker.  If the configuration is provided then a message broker with that configuration will be created, if a message broker has already been created with the same configuration then it will return the previously created message broker.  If the configuration is not provided then the configuration will be automatically searched for in the following order:

    -   Look at the value set specified in a call to and uses that assuming it's not null or empty

        ..  tabs::

            ..  group-tab:: C++

                SetMessageBrokerDefaultConfig(const char* config)

            ..  group-tab:: Python

                messagebroker.set_default_config(config : str)

    -   Looks for a value defined in environment variable "MESSAGE_BROKER_CONFIG_FILE".  The value in this environment variable should contain the absolute path to a file that contains the configuration information.
    -   Uses the empty configuration value of "{}";

In general we recommend to not provide the configuration information when creating the event broker.  Instead, near the entry point of your application set the configuration through the Set Default Config API.  This will allow your components to automatically pick up the configuration and set by your application and thus retrieving a handle to the same message broker instance without needing prior knowledge of the configuration.

Example
-------

..  tabs::

    ..  group-tab:: C++

        .. note:: link against libpanorama.aws.so

        .. literalinclude:: ../../../samples/C++/MessageBroker/message_broker_sample.cpp
            :language: cpp
            :linenos:

    ..  group-tab:: Python

        .. literalinclude:: ../../../samples/Python/MessageBroker/message_broker_sample.py
            :language: python
            :linenos:


Full API
--------

..  tabs::

        ..  group-tab:: C++

            **Interfaces**

            .. doxygenstruct:: Panorama::IMessageBrokerEventHandler
                :members:

            .. doxygenstruct:: Panorama::IMessageBroker
                :members:

            .. doxygenstruct:: Panorama::IPayload
                :members:

            .. doxygenstruct:: Panorama::IBatchPayload
                :members:

            .. doxygenstruct:: Panorama::IVideoPayload
                :members:

            **Factory Methods**

            .. doxygenfunction:: Panorama::MessageBroker::Create(IMessageBroker** ppObj, ICredentialProvider* credentials = nullptr, const char* config = nullptr, bool unique = false)
            .. doxygenfunction:: Panorama::MessageBroker::LoadConfiguration(IBuffer** ppObj)
            .. doxygenfunction:: Panorama::MessageBroker::SetDefaultConfig(const char* config)
            .. doxygenfunction:: Panorama::MessageBroker::CreatePayload(IPayload** ppObj, const char* contents)
            .. doxygenfunction:: Panorama::MessageBroker::CreatePayload(IPayload** ppObj, IBuffer* contents)
            .. doxygenfunction:: Panorama::MessageBroker::CreateBatchPayload(IBatchPayload** ppObj)


        ..  group-tab:: Python

            **Classes**

            .. autoclass:: panorama.messagebroker.MessageBroker
                :members:
                :undoc-members:

            **Factory Methods**

            .. automethod:: panorama.messagebroker.create
            .. automethod:: panorama.messagebroker.set_default_config
            .. automethod:: panorama.messagebroker.create_payload_from_string
            .. automethod:: panorama.messagebroker.create_payload_from_buffer
