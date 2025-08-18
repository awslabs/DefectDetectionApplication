#include <iostream>

#include <Panorama/trace.h>
#include <Panorama/aws.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/eventing.h>

using namespace Panorama;

int main()
{
    ADD_CONSOLE_TRACE;
    HRESULT hr = S_OK;

    ComPtr<IProtocolClient> client;
    ComPtr<ICredentialProvider> credential_provider;
    ComPtr<IMqttSubscription> subscription;

    // Get the default AWS credential provider, uses temporary SigV4 credentials
    ComPtr<ICredentialProvider> creds = Panorama_Aws::DefaultCredentialProvider();

    // Create the MQTT Protocol client
    // You'll need to update the endpoint and region to something that is appropriate for you
    CHECKHR(Panorama_Aws::MqttProtocolClient(client.AddressOf(), "a5h6960s3xow6-ats.iot.us-west-2.amazonaws.com", "us-west-2", creds));

    // Subscribe to messages from the client
    CHECKHR(Panorama_Aws::MqttSubscription(subscription.AddressOf(), "mqtt_subscribe_sample"));
    CHECKHR(client->Subscribe(subscription, [&](IPayload* recieved_mqtt_message)
    {
        TraceInfo("Received message %s", recieved_mqtt_message->SerializeAsString());
    }));

    // The Subscribe method returns a cancellation token which can be used for unsubscribing.
    int32_t token = hr;

    // Publish the message asynchronously
    do
    {
        std::string message;
        std::cout << "\nEnter message to publish: ('q' to exit)";
        std::cin >> message;

        if(message.compare("q") == 0)
        {
            break;
        }

        // Create a message that can be published to MQTT
        ComPtr<IMqttMessage> mqtt_message;
        CHECKHR(Panorama_Aws::MqttMessage(mqtt_message.AddressOf(), message.c_str(), "mqtt_publish_sample"));
        CHECKHR(client->PublishAsync(mqtt_message, [&](const char* publisher, IProtocolMessage* published, bool succesful)
        {
            TraceInfo("Message %s completed publishing on protocol %s.", succesful ? "successfully" : "unsuccessfully", publisher);
        }));

        TraceInfo("Message queued for publishing");
    }while(true);

    // Unsubscribe from the topic.  
    // This step isn't necessary as unsubscription happens when the mqtt client is destroyed.
    // Here for completeness
    PEEKHR(client->Unsubscribe(token));

    return hr;
}