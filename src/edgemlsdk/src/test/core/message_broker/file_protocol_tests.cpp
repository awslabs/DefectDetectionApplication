
#include <fstream>
#include <nlohmann/json.hpp>

#include <gtest/gtest.h>
#include <Panorama/comptr.h>
#include <Panorama/aws.h>
#include <TestUtils.h>
#include <filesystem_safe.h>

using namespace std;
using namespace Panorama;

void CheckFile(std::string filename, uint8_t* contents)
{
    fs::path filePath(filename);
    ASSERT_TRUE(fs::exists(filePath));

    FILE* fptr = fopen(filename.c_str(), "rb");
    fseek(fptr, 0, SEEK_END);
    long sz = ftell(fptr);
    if(sz <= 0)
    {
        fclose(fptr);
        ASSERT_TRUE(false);
    }
    fseek(fptr, 0, SEEK_SET);

    std::vector<uint8_t> data(sz);

    size_t bytes_read = fread(data.data(), sizeof(uint8_t), static_cast<size_t>(sz), fptr);
    fclose(fptr);
    ASSERT_EQ(bytes_read, static_cast<size_t>(sz));
    ASSERT_TRUE(memcmp(contents, data.data(), data.size()) == 0);
}

void Publish()
{
    HRESULT hr = S_OK;

    // Create the client
    ComPtr<IProtocolClient> client;
    ASSERT_S(MessageBroker::FileProtocolClient(client.AddressOf()));

    // Create a payload to publish
    ComPtr<IPayload> payload;
    ASSERT_S(MessageBroker::CreatePayload(payload.AddressOf(), "some random string"));
    ASSERT_S(payload->SetCorrelationId("my-correlation-id"));

    // Get buffer of payload for publish validation
    ComPtr<IBuffer> buffer;
    ASSERT_S(payload->Serialize(buffer.AddressOf()));

    std::string id = payload->Id();
    std::string ts = std::to_string(payload->Timestamp());
    {
        ComPtr<IFileProtocolMessage> message;
        ASSERT_S(MessageBroker::FileProtocolMessage(message.AddressOf(), payload, (BuildDirectory() + "/file_protocol_test").c_str(), "test_file.txt"));
        ASSERT_S(client->Publish(message));
        CheckFile(BuildDirectory() + "/file_protocol_test/test_file.txt", buffer->Data());
    }
    
    {
        ComPtr<IFileProtocolMessage> message;
        ASSERT_S(MessageBroker::FileProtocolMessage(message.AddressOf(), payload, (BuildDirectory() + "/file_protocol_test").c_str(), "${timestamp}_test_file-${id}-${c_id}"));
        ASSERT_S(client->Publish(message));
        std::string generated_file = "/file_protocol_test/" + ts + "_test_file-" + id + "-my-correlation-id"; 
        CheckFile(BuildDirectory() + generated_file, buffer->Data());
    }

    {
        ComPtr<IFileProtocolMessage> message;
        ASSERT_S(MessageBroker::FileProtocolMessage(message.AddressOf(), payload, (BuildDirectory() + "/file_protocol_test").c_str(), "test_file_2"));
        ManualResetEvent published_signal;
        ASSERT_S(client->PublishAsync(message, [&](const char* protocol, IProtocolMessage* publshed, bool successful)
        {
            ASSERT_TRUE(strcmp(protocol, "file") == 0);
            ASSERT_TRUE(successful);
            ASSERT_TRUE(publshed == message.Ptr());
            published_signal.Set();
        }));

        ASSERT_TRUE(published_signal.WaitFor(1000));
        CheckFile(BuildDirectory() + "/file_protocol_test/test_file_2", buffer->Data());
    }

}

void MessageBrokerIntegration()
{
    HRESULT  hr = S_OK;
    std::string config = 
    "{                                                                                          "
    "    \"targets\": [                                                                         "
    "        {                                                                                  "
    "            \"protocol\": \"file\",                                                        "
    "            \"name\": \"test\",                                                            "
    "            \"file_options\": {                                                            "
    "            }                                                                              "
    "        }                                                                                  "
    "    ],                                                                                     "
    "    \"pipes\": [                                                                           "
    "        {                                                                                  "
    "            \"message_id\": \"test_message\",                                              "
    "            \"destinations\": [                                                            "
    "                {                                                                          "
    "                    \"target_name\": \"test\",                                             "
    "                    \"file_message_options\": {                                            "
    "                        \"directory\": \"" + BuildDirectory() + "/file_protocol_test\",    "
    "                        \"filename\": \"${timestamp}_test_file-${id}-${c_id}.txt\"         "
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
    ASSERT_S(MessageBroker::CreatePayload(payload.AddressOf(), "some random string"));
    ASSERT_S(payload->SetCorrelationId("c"));

    // Get buffer of payload for publish validation
    ComPtr<IBuffer> buffer;
    ASSERT_S(payload->Serialize(buffer.AddressOf()));

    // Determine the name of the file that should be generated
    std::string id = payload->Id();
    std::string ts = std::to_string(payload->Timestamp());
    std::string generated_file = "/file_protocol_test/" + ts + "_test_file-" + id + "-c.txt"; 

    ASSERT_S(broker->Publish("test_message", payload));
    CheckFile(BuildDirectory() + generated_file, buffer->Data());
}

TEST(Core, FileProtocolTests)
{
    HRESULT hr = S_OK;

    fs::path dir(BuildDirectory() + "/file_protocol_test");
    if(fs::exists(dir))
    {
        std::string rmdir = "rm -r " + BuildDirectory() + "/file_protocol_test";
        ASSERT_S(system(rmdir.c_str()));
    }

    Publish();
    MessageBrokerIntegration();
}