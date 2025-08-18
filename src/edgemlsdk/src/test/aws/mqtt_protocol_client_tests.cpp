
#include <thread>
#include <fstream>

#include <gtest/gtest.h>
#include <Panorama/comptr.h>
#include <Panorama/aws.h>
#include <TestUtils.h>

using namespace std;
using namespace Panorama;

void InputChecking()
{
    TraceInfo("===== InputChecking =====");
    HRESULT hr = S_OK;
    ComPtr<ICredentialProvider> creds = Panorama_Aws::DefaultCredentialProvider();

    ComPtr<IProtocolClient> client;
    ASSERT_F(Panorama_Aws::MqttProtocolClient(client.AddressOf(), nullptr, "non-null", creds));
    ASSERT_F(Panorama_Aws::MqttProtocolClient(client.AddressOf(), "non-null", nullptr, creds));
    ASSERT_F(Panorama_Aws::MqttProtocolClient(client.AddressOf(), "non-null", "non-null", nullptr));
    ASSERT_F(Panorama_Aws::MqttProtocolClient(client.AddressOf(), "non-null", "non-null", creds));
}

void PublishSubscribe()
{
    TraceInfo("===== PublishSubscribe =====");
    HRESULT hr = S_OK;
    ComPtr<ICredentialProvider> creds = Panorama_Aws::DefaultCredentialProvider();

    // Create client
    ComPtr<IProtocolClient> client;
    ASSERT_S(Panorama_Aws::MqttProtocolClient(client.AddressOf(), "a5h6960s3xow6-ats.iot.us-west-2.amazonaws.com", "us-west-2", creds));

    // Create subscription
    AutoResetEvent message_received;
    ComPtr<IMqttSubscription> subscription;
    ASSERT_S(Panorama_Aws::MqttSubscription(subscription.AddressOf(), "my_test_topic"));
    ASSERT_S(client->Subscribe(subscription, [&](IPayload* msg)
    {
        ASSERT_FALSE(strcmp(msg->SerializeAsString(), "hello world"));
        message_received.Set();
    }));
    int32_t token = hr;

    // Subscribe doesn't appear to be immediately hooked up on server side, give it a second to sort that out
    ThreadSleep(1000);

    // Create a message to publish
    ComPtr<IMqttMessage> message;
    ASSERT_S(Panorama_Aws::MqttMessage(message.AddressOf(), "hello world", "my_test_topic"));

    // Publish the message synchronously
    ASSERT_S(client->Publish(message));
    ASSERT_TRUE(message_received.WaitFor(3000));

    // Publish the message asynchronously
    ASSERT_S(client->PublishAsync(message, [&](const char* publisher, IProtocolMessage* msg, bool successful)
    {
        ASSERT_FALSE(strcmp(publisher, "mqtt"));
        ASSERT_EQ(msg, message.Ptr());
        ASSERT_TRUE(successful);
    }));
    ASSERT_TRUE(message_received.WaitFor(3000));

    // Unsubcribe from topic
    client->Unsubscribe(token);
    ASSERT_S(client->Publish(message));
    ASSERT_FALSE(message_received.WaitFor(2000));
}

void Reconnect()
{
    TraceInfo("===== Reconnect =====");
    HRESULT hr = S_OK;

    ComPtr<ICredentialProvider> creds = Panorama_Aws::DefaultCredentialProvider();

    // Input Checking
    ComPtr<IProtocolClient> client;
    ASSERT_S(Panorama_Aws::MqttProtocolClient(client.AddressOf(), "a5h6960s3xow6-ats.iot.us-west-2.amazonaws.com", "us-west-2", creds));

    // Subscribe
    AutoResetEvent messageReceived;
    ComPtr<IMqttSubscription> subscription;
    ASSERT_S(Panorama_Aws::MqttSubscription(subscription.AddressOf(), "my_test_topic"));
    ASSERT_S(client->Subscribe(subscription, [&](IPayload* msg)
    {
        ASSERT_FALSE(strcmp(msg->SerializeAsString(), "hello world"));
        messageReceived.Set();
    }));

    // Subscribe doesn't appear to be immediately hooked up on server side, give it a second to sort that out
    ThreadSleep(1000);

    //Reconnect, should still receive messages on subscriptions
    ASSERT_S(client->Reconnect());

    // Subscribe doesn't appear to be immediately hooked up on server side, give it a second to sort that out
    ThreadSleep(1000);

    // Create a message to publish
    ComPtr<IMqttMessage> message;
    ASSERT_S(Panorama_Aws::MqttMessage(message.AddressOf(), "hello world", "my_test_topic"));

    // Publish the message synchronously
    ASSERT_S(client->Publish(message));
    ASSERT_TRUE(messageReceived.WaitFor(3000));
}

void MessageBrokerIntegration()
{
    TraceInfo("===== MessageBrokerIntegration =====");
    HRESULT  hr = S_OK;
    std::string config = 
    "{                                                                                  "
    "    \"targets\": [                                                                 "
    "        {                                                                          "
    "            \"protocol\": \"mqtt\",                                                "
    "            \"name\": \"test-mqtt\",                                               "
    "            \"mqtt_options\": {                                                    "
    "                \"endpoint\": \"a5h6960s3xow6-ats.iot.us-west-2.amazonaws.com\",   "
    "                \"region\": \"us-west-2\",                                         "
    "                \"client-id\": \"some-client-id\"                                  "
    "            },                                                                     "
    "            \"mqtt_subscriptions\": [                                              "
    "                {                                                                  "
    "                    \"subscription_id\": \"mqtt_test_subscription\",               "
    "                    \"topic\": \"test_subscription\"                               "
    "                }                                                                  "
    "            ]                                                                      "
    "        }                                                                          "
    "    ],                                                                             "
    "    \"pipes\": [                                                                   "
    "        {                                                                          "
    "            \"message_id\": \"test_message\",                                      "
    "            \"destinations\": [                                                    "
    "                {                                                                  "
    "                    \"target_name\": \"test-mqtt\",                                "
    "                    \"mqtt_message_options\": {                                    "
    "                        \"topic\": \"test_subscription\"                           "
    "                    }                                                              "
    "                }                                                                  "
    "            ]                                                                      "
    "        }                                                                          "
    "    ]                                                                              "
    "}                                                                                  ";

    ComPtr<ICredentialProvider> creds = Panorama_Aws::DefaultCredentialProvider();
    ASSERT_TRUE(creds != nullptr);

    ComPtr<IMessageBroker> broker;
    ASSERT_S(MessageBroker::Create(broker.AddressOf(), creds, config.c_str(), true));
    ASSERT_S(broker->Initialize());

    AutoResetEvent message_received;
    ASSERT_S(broker->Subscribe("mqtt_test_subscription", [&](IPayload* payload)
    {
        ASSERT_TRUE(strcmp(payload->SerializeAsString(), "hello world") == 0);
        message_received.Set();
    }));

    // wait for subscription to get hooked up on server side
    ThreadSleep(1000);

    ASSERT_S(broker->Publish("test_message", "hello world"));
    ASSERT_TRUE(message_received.WaitFor(3000));
}

TEST(AWSTests, MqttProtocolClientTests)
{
    InputChecking();
    PublishSubscribe();
    Reconnect();
    MessageBrokerIntegration();
}