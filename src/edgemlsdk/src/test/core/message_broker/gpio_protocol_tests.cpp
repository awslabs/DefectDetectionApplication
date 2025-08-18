
#include <fstream>
#include <nlohmann/json.hpp>

#include <gtest/gtest.h>
#include <Panorama/comptr.h>
#include <Panorama/aws.h>
#include <TestUtils.h>
#include "periphery/gpio.h"

using namespace std;
using namespace Panorama;

void Publish()
{
    HRESULT hr = S_OK;

    // Create the client
    ComPtr<IProtocolClient> client;
    ASSERT_S(MessageBroker::GPIOProtocolClient(client.AddressOf()));

    // Create a payload to publish
    ComPtr<IPayload> payload;
    // 1 indicates anomaly.
    std::vector<uint8_t> payload_data = {1} ;
    ASSERT_S(MessageBroker::CreatePayload(payload.AddressOf(), reinterpret_cast<const char*>(payload_data.data())));
    ASSERT_S(payload->SetCorrelationId("my-correlation-id"));

    // Get buffer of payload for publish validation
    ComPtr<IBuffer> buffer;
    ASSERT_S(payload->Serialize(buffer.AddressOf()));

    std::string id = payload->Id();
    std::string ts = std::to_string(payload->Timestamp());
    std::vector<int64_t> pins = { 225, 224 };
    std::vector<int64_t> pulse_width_ms = { 500, 500 };
    // All GPIO Publishes will fail since, there is no actual gpio in test docker environment.
    {
        ComPtr<IGPIOProtocolMessage> message;
        ASSERT_S(MessageBroker::GPIOProtocolMessage(message.AddressOf(), payload, "All;Anomaly", "GPIO.RISING;GPIO.RISING", pins.data(), pulse_width_ms.data(), pins.size()));
        ASSERT_F(client->Publish(message));
    }
    
    {
        ComPtr<IGPIOProtocolMessage> message;
        ASSERT_S(MessageBroker::GPIOProtocolMessage(message.AddressOf(), payload, "All;Anomaly", "GPIO.RISING;GPIO.RISING", pins.data(), pulse_width_ms.data(), pins.size()));
        ASSERT_F(client->PublishAsync(message,nullptr));
    }
}

void MessageBrokerIntegration()
{
    HRESULT  hr = S_OK;
    std::string config = 
    "{                                                                                          "
    "    \"targets\": [                                                                         "
    "        {                                                                                  "
    "            \"protocol\": \"gpio\",                                                        "
    "            \"name\": \"test\",                                                            "
    "            \"gpio_options\": {                                                            "
    "            }                                                                              "
    "        }                                                                                  "
    "    ],                                                                                     "
    "    \"pipes\": [                                                                           "
    "        {                                                                                  "
    "            \"message_id\": \"test-gpio-message_${rule}_${signal_type}_${pin}_${pulse_width_ms}\",                                              "
    "            \"destinations\": [                                                            "
    "                {                                                                          "
    "                    \"target_name\": \"test\",                                             "
    "                    \"gpio_message_options\": {                                            "
    "                        \"rules\": \"${rule}\",                                             "
    "                        \"signal_types\": \"${signal_type}\",                               "
    "                        \"pins\": \"${pin}\",                                               "
    "                        \"pulse_width_ms\": \"${pulse_width_ms}\"                               "
    "                    }                                                                      "
    "                }                                                                          "
    "            ]                                                                              "
    "        }                                                                                  "
    "    ]                                                                                      "
    "}                                                                                          ";

    
    ComPtr<IMessageBroker> broker;
    ASSERT_S(MessageBroker::Create(broker.AddressOf(), nullptr, config.c_str(), true));
    ASSERT_S(broker->Initialize());

    // Create a payload to publish
    ComPtr<IPayload> payload;
    // 1 indicates anomaly.
    std::vector<uint8_t> payload_data = {1} ;
    ASSERT_S(MessageBroker::CreatePayload(payload.AddressOf(), reinterpret_cast<const char*>(payload_data.data())));
    ASSERT_S(payload->SetCorrelationId("c"));

    // Get buffer of payload for publish validation
    ComPtr<IBuffer> buffer;
    ASSERT_S(payload->Serialize(buffer.AddressOf()));

    // Determine the name of the file that should be generated
    std::string id = payload->Id();
    std::string ts = std::to_string(payload->Timestamp());

    ASSERT_F(broker->Publish("test-gpio-message_All;Anomaly_GPIO.RISING;GPIO.RISING_225;224_500;500", payload));
}

TEST(Core, GPIOProtocolTests)
{
    HRESULT hr = S_OK;
    Publish();
    MessageBrokerIntegration();
}