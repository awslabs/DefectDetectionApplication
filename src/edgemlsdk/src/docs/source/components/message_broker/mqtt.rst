.. _mqtt_protocol_client:

====================
Mqtt Protocol Client
====================

Overview
--------

This protocol client interfaces to a MQTT endpoint in AWS.

Message Broker Integration
--------------------------

Target Parameters
^^^^^^^^^^^^^^^^^

-   **protocol**

    .. code-block:: json

        "protocol": "mqtt"

-   **mqtt_options**

    -   region: [Required] AWS Region of the S3 bucket you wish to publish too
    -   endpoint: [Required] Endpoint of the MQTT server
    -   client-id: [Optional] Id of your connecting client.  Defaults to a randomly created UUID.  If provided you are responsible for ensuring this is unique between connections.

    .. code-block:: json

        "mqtt_options": {
            "region": "<your-aws-region>",
            "endpoint": "<your-mqtt-endpoint>",
            "client-id": "<your-client-id>"
        }

-   **mqtt_subscriptions**

    -   topic:  The topic to subscribe too
    -   subscription-id:  The id of the callback to invoke

    .. code-block:: json

        "mqtt_subscriptions": [
            {
                "subscription_id": "your-subscription-id",
                "topic": "your-mqtt-topic"
            }
        ]


MQTT Message Options
^^^^^^^^^^^^^^^^^^

-   topic:  The MQTT topic to publish too

    .. code-block:: json

        "mqtt_message_options": {
            "topic": "another-mqtt-topic"
        }


Message Broker Config Sample
^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: json

    {
        "targets": [
            {
                "protocol": "mqtt",
                "name": "sample-mqtt",
                "mqtt_options": {
                    "endpoint": "<your-endpoint>",
                    "region": "<your-region>",
                    "client-id": "<your-client-id>"
                },
                "mqtt_subscriptions": [
                    {
                        "subscription_id": "your-subscription-id",
                        "topic": "your-mqtt-topic"
                    }
                ]
            }
        ],
        "pipes": [
            {
                "message_id": "your-message",
                "destinations": [
                    {
                        "target_name": "sample-mqtt",
                        "mqtt_message_options": {
                            "topic": "another-mqtt-topic"
                        }
                    }
                ]
            }
        ]
    }

Sample
------

This sample instantiates a Mqtt Protocol Client and demonstrates how to subscribe and publish to topics.

..  tabs::

    ..  group-tab:: C++

        .. note:: link against libpanorama.aws.so

        .. literalinclude:: ../../../samples/C++/MessageBroker/mqtt.cpp
            :language: cpp
            :linenos:

    ..  group-tab:: Python

        .. literalinclude:: ../../../samples/Python/MessageBroker/mqtt_protocol_client_sample.py
            :language: python
            :linenos:


API
---

..  tabs::

    ..  group-tab:: C++

        **Interfaces**

        .. doxygenstruct:: Panorama::IMqttMessage
            :members:

        .. doxygenstruct:: Panorama::IMqttSubscription
            :members:

        **Factory Methods**

        .. doxygenfunction:: Panorama::Panorama_Aws::MqttProtocolClient(IProtocolClient** ppObj, const char* endpoint, const char* region, ICredentialProvider* credential_provider)
        .. doxygenfunction:: Panorama::Panorama_Aws::MqttMessage(IMqttMessage** ppObj, const char* contents, const char* topic)
        .. doxygenfunction:: Panorama::Panorama_Aws::MqttMessage(IMqttMessage** ppObj, IPayload* payload, const char* topic)
        .. doxygenfunction:: Panorama::Panorama_Aws::MqttSubscription(IMqttSubscription** ppObj, const char* topic)

    ..  group-tab:: Python

        .. autoclass:: panorama.messagebroker.MqttProtocolClient
                :members:
                :undoc-members: