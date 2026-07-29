#include <Panorama/flowcontrol.h>
#include <Panorama/trace.h>
#include <Panorama/message_broker.h>
#include <Panorama/aws.h>

using namespace Panorama;

int main()
{

    ADD_CONSOLE_TRACE;

    HRESULT hr = S_OK;

    /*
        The following configuration has the following behavior:
        - Two targets created: MQTT and S3
        - Subscribing to subscription-id `test-subscription` will receive messages published onto mqtt topic 'broker-test-subscription'
        - Messages published with message-id 'test_message' will be published onto mqtt topic 'broker-test-publish'
        - Messages published with message-id 'big_data' will be saved to S3 at s3://panorama-sdk-v2-artifacts/test/broker_sample
            - Optional "overwrite" flag is not specified, so default behavior will be to overwrite any existing contents of the bucket/key
    */
    std::string config = 
    "{                                                                                  "
    "    \"targets\": [                                                                 "
    "        {                                                                          "
    "            \"protocol\": \"mqtt\",                                                "
    "            \"name\": \"test-mqtt\",                                               "
    "            \"mqtt_options\": {                                                    "
    "                \"endpoint\": \"a5h6960s3xow6-ats.iot.us-west-2.amazonaws.com\",   "
    "                \"region\": \"us-west-2\"                                          "
    "            },                                                                     "
    "            \"mqtt_subscriptions\": [                                              "
    "                {                                                                  "
    "                    \"subscription_id\": \"test-subscription\",                    "
    "                    \"topic\": \"broker-test-subscription\"                        "
    "                }                                                                  "
    "            ]                                                                      "
    "        },                                                                         "
    "        {                                                                          "
    "            \"protocol\": \"s3\",                                                  "
    "            \"name\": \"test-s3\",                                                 "
    "            \"s3_options\": {                                                      "
    "                \"region\": \"us-west-2\"                                          "
    "            }                                                                      "
    "        }                                                                          "
    "    ],                                                                             "
    "    \"pipes\": [                                                                   "
    "        {                                                                          "
    "            \"message_id\": \"test_message\",                                      "
    "            \"destinations\": [                                                    "
    "                {                                                                  "
    "                    \"target_name\": \"test-mqtt\",                                "
    "                    \"mqtt_message_options\": {                                    "
    "                        \"topic\": \"broker-test-publish\"                         "
    "                    }                                                              "
    "                }                                                                  "
    "            ]                                                                      "
    "        },                                                                         "
    "        {                                                                          "
    "            \"message_id\": \"big_data\",                                          "
    "            \"destinations\": [                                                    "
    "                {                                                                  "
    "                    \"target_name\": \"test-s3\",                                  "
    "                    \"s3_message_options\": {                                      "
    "                        \"bucket\": \"panorama-sdk-v2-artifacts\",                 "
    "                        \"key\": \"test/broker_sample\"                            "
    "                    }                                                              "
    "                }                                                                  "
    "            ]                                                                      "
    "        }                                                                          "
    "    ]                                                                              "
    "}                                                                                  ";

    // Get the credential provider for AWS credentials set in your environment variables
    ComPtr<ICredentialProvider> credentials = Panorama_Aws::DefaultCredentialProvider();
    CHECKNULL(credentials, E_FAIL);

    // Set the default configuration to the string defined above
    MessageBroker::SetDefaultConfig(config.c_str());

    // Create the message broker without expclitly passing the configuration data
    ComPtr<IMessageBroker> broker;
    CHECKHR(MessageBroker::Create(broker.AddressOf(), credentials));
    
    // Broker initialize must be called to instantiate target protocols and hook up pipes
    // Safe to call multiple times.
    CHECKHR(broker->Initialize());

    // Subscribe to test-subscription
    // Will be invoked when mqtt topic 'broker-test-subscription' is published too
    CHECKHR(broker->Subscribe("test-subscription", [&](IPayload* payload)
    {
        TraceInfo("Received message: %s", payload->SerializeAsString()..c_str());
    }));
    int32_t test_subscription_cancellation_token = hr;

    // In addition to publishing/subscribing to predefined targets you can locally subscribe and publish
    // Here we are subscribing to the "test_message" subscription-id.  Will be invoked (in addition to routing to mqtt)
    // when a publish happens with message_id = `test_message`
    CHECKHR(broker->Subscribe("test_message", [&](IPayload* payload)
    {
        TraceInfo("Received locally: %s", payload->SerializeAsString().c_str());
    }));
    int32_t test_message_cancellation_token = hr;

    // Create some payloads for publishing
    ComPtr<IPayload> small, large;
    CHECKHR(MessageBroker::CreatePayload(small.AddressOf(), "hello world"));

    ComPtr<IBuffer> large_data;
    CHECKHR(Buffer::Create(large_data.AddressOf(), 1024)); // 1 KB
    CHECKHR(MessageBroker::CreatePayload(large.AddressOf(), large_data));

    // Publish message 'test_message'
    // Will be routed to mqtt protocol client to publish to mqtt topic 'broker-test-publish'
    CHECKHR(broker->PublishAsync("test_message", small, [&](const char* protocol, const char* message, IPayload* payload, bool successful)
    {
        if(successful)
        {
            TraceInfo("Successfuly published message '%s' on protocol '%s'", message, protocol);
        }
        else
        {
            TraceError("Failed to publish message to '%s' on protocol '%s'", message, protocol);
        }
    }));

    // Publish message 'big_data'
    // Will be routed to S3 client to publish to save to s3://panorama-sdk-v2-artifacts/test/broker_sample
    CHECKHR(broker->PublishAsync("big_data", large, [&](const char* protocol, const char* message, IPayload* payload, bool successful)
    {
        if(successful)
        {
            TraceInfo("Successfuly published message '%s' on protocol '%s'", message, protocol);
        }
        else
        {
            TraceError("Failed to publish message to '%s' on protocol '%s'", message, protocol);
        }
    }));

    TraceInfo("Press any key to exit");
    char ret = getchar();

    // Not necessary to do this as unsubscribing will happen to dtor of message broker
    // However, here for completeness
    PEEKHR(broker->Unsubscribe(test_subscription_cancellation_token));
    PEEKHR(broker->Unsubscribe(test_message_cancellation_token));

    return hr;
}