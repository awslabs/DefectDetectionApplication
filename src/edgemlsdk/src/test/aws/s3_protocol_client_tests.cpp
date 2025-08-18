
#include <nlohmann/json.hpp>

#include <sstream>
#include <gtest/gtest.h>
#include <Panorama/comptr.h>
#include <Panorama/aws.h>
#include <TestUtils.h>

using namespace std;
using namespace Panorama;

void _sendMessageBrokerMessage(IPayload* payload, const std::string &bucket, const std::string &key, bool overwriteFlag = true, bool batchPayloadExpansionFlag = true)
{
    std::string config = 
    "{                                                                                  "
    "    \"targets\": [                                                                 "
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
    "                    \"target_name\": \"test-s3\",                                  "
    "                    \"s3_message_options\": {                                      "
    "                        \"bucket\": \"{REPLACE_WITH_BUCKET}\",                     "
    "                        \"key\": \"{REPLACE_WITH_KEY}\",                           "
    "                        \"overwrite\": {REPLACE_WITH_OVERWRITE},                   "
    "                        \"batch_payload_expansion\": {REPLACE_WITH_BATCH_FLAG}     "
    "                    }                                                              "
    "                }                                                                  "
    "            ]                                                                      "
    "        }                                                                          "
    "    ]                                                                              "
    "}                                                                                  ";
    
    size_t p = config.find("{REPLACE_WITH_BUCKET}");
    config.replace(p, 21 /*length of {REPLACE_WITH_BUCKET}*/, bucket);
    p = config.find("{REPLACE_WITH_KEY}");
    config.replace(p, 18 /*length of {REPLACE_WITH_KEY}*/, key);
    p = config.find("{REPLACE_WITH_OVERWRITE}");

    config.replace(p, 24 /*length of {REPLACE_WITH_OVERWRITE}*/, (overwriteFlag ? "true" : "false"));
    p = config.find("{REPLACE_WITH_BATCH_FLAG}");
    config.replace(p, 25 /*length of {REPLACE_WITH_BATCH_FLAG}*/, (batchPayloadExpansionFlag ? "true" : "false"));

    HRESULT hr = S_OK;

    ComPtr<ICredentialProvider> creds = Panorama_Aws::DefaultCredentialProvider();
    ASSERT_TRUE(creds != nullptr);

    ComPtr<IMessageBroker> broker;
    ASSERT_S(MessageBroker::Create(broker.AddressOf(), creds, config.c_str(), true));
    ASSERT_S(broker->Initialize());
    ASSERT_S(broker->Publish("test_message", payload));
}

void _sendMessageBrokerMessage(const std::string &messageContent, const std::string &bucket, const std::string &key, bool overwriteFlag = true, bool batchPayloadExpansionFlag = true)
{
    HRESULT hr = S_OK;
    ComPtr<IPayload> payload;
    ASSERT_S(MessageBroker::CreatePayload(payload.AddressOf(), messageContent.c_str()));
    _sendMessageBrokerMessage(payload, bucket, key, overwriteFlag, batchPayloadExpansionFlag);
}

void _sendProtocolClientMessage(IPayload* payload, const std::string &bucket, const std::string &key, bool overwriteFlag = true, bool batchPayloadExpansionFlag = true)
{
    HRESULT hr = S_OK;
    ComPtr<ICredentialProvider> creds = Panorama_Aws::DefaultCredentialProvider();
    ASSERT_TRUE(creds != nullptr);
    ComPtr<IProtocolClient> client;

    ASSERT_S(Panorama_Aws::S3ProtocolClient(client.AddressOf(), "us-west-2", creds));
    ComPtr<IS3Message> msg;
    ASSERT_S(Panorama_Aws::S3Message(msg.AddressOf(), payload, bucket.c_str(), key.c_str(), overwriteFlag, batchPayloadExpansionFlag));
    AutoResetEvent published_message;
    ASSERT_S(client->PublishAsync(msg, [&](const char* publisher, IProtocolMessage* published, bool succeeded)
    {
        ASSERT_FALSE(strcmp(publisher, "s3"));
        ASSERT_TRUE(succeeded);
        published_message.Set();
    }));
    ASSERT_TRUE(published_message.WaitFor(3000));
}

void _sendProtocolClientMessage(const std::string &messageContent, const std::string &bucket, const std::string &key, bool overwriteFlag = true, bool batchPayloadExpansionFlag = true)
{
    HRESULT hr = S_OK;
    ComPtr<IPayload> payload;
    ASSERT_S(MessageBroker::CreatePayload(payload.AddressOf(), messageContent.c_str()));
    _sendProtocolClientMessage(payload, bucket, key, overwriteFlag, batchPayloadExpansionFlag);
}

void _checkContentTimestampField(const std::string &expectedTimestamp, const std::string &bucket, const std::string key)
{
    HRESULT hr = S_OK;
    ComPtr<ICredentialProvider> creds = Panorama_Aws::DefaultCredentialProvider();
    ASSERT_TRUE(creds != nullptr);
    ComPtr<IPropertyDelegate> s3PropertyDelegate;
    ASSERT_S(CreateS3PropertyDelegate(s3PropertyDelegate.AddressOf(), bucket.c_str(), key.c_str(), "us-west-2", creds));
    ComPtr<IProperty> timestamp;
    ASSERT_S(s3PropertyDelegate->GetProperty(timestamp.AddressOf(), "timestamp"));
    ASSERT_FALSE(strcmp(timestamp.QueryInterface<IStringProperty>()->Get(), expectedTimestamp.c_str()));
}

void _SendAndCheckProtocolClientMessageFlags(std::string messageTimestamp, std::string expectedTimestamp, bool setExplicitOverwriteFlag, bool overwriteFlag = true, bool setExplicitBatchPayloadFlag = false, bool batchPayloadExpansionFlag = false)
{
    HRESULT hr = S_OK;

    ComPtr<ICredentialProvider> creds = Panorama_Aws::DefaultCredentialProvider();
    ASSERT_TRUE(creds != nullptr);
    ComPtr<IProtocolClient> client;

    // Input checking
    ASSERT_F(Panorama_Aws::S3ProtocolClient(client.AddressOf(), nullptr, creds));
    ASSERT_F(Panorama_Aws::S3ProtocolClient(client.AddressOf(), "non-null", nullptr));
    ASSERT_S(Panorama_Aws::S3ProtocolClient(client.AddressOf(), "us-west-2", creds));

    nlohmann::json jObj;
    jObj["timestamp"] = messageTimestamp;

    ComPtr<IS3Message> msg;
    if (setExplicitOverwriteFlag)
    {
        if (setExplicitBatchPayloadFlag)
        {
            ASSERT_S(Panorama_Aws::S3Message(msg.AddressOf(), jObj.dump().c_str(), "panorama-sdk-v2-artifacts", "test/s3msgbroker", overwriteFlag, batchPayloadExpansionFlag));
        }
        else
        {
            ASSERT_S(Panorama_Aws::S3Message(msg.AddressOf(), jObj.dump().c_str(), "panorama-sdk-v2-artifacts", "test/s3msgbroker", overwriteFlag));
        }
    }
    else
    {
        if (setExplicitBatchPayloadFlag)
        {
            // Need to explicitly provide overwrite flag here too.
            ASSERT_S(Panorama_Aws::S3Message(msg.AddressOf(), jObj.dump().c_str(), "panorama-sdk-v2-artifacts", "test/s3msgbroker", overwriteFlag, batchPayloadExpansionFlag));
        }
        else
        {
            ASSERT_S(Panorama_Aws::S3Message(msg.AddressOf(), jObj.dump().c_str(), "panorama-sdk-v2-artifacts", "test/s3msgbroker"));
        }
    }

    AutoResetEvent published_message;
    ASSERT_S(client->PublishAsync(msg, [&](const char* publisher, IProtocolMessage* published, bool succeeded)
    {
        ASSERT_FALSE(strcmp(publisher, "s3"));
        ASSERT_TRUE(succeeded);
        published_message.Set();
    }));

    ASSERT_TRUE(published_message.WaitFor(3000));
    _checkContentTimestampField(expectedTimestamp, "panorama-sdk-v2-artifacts", "test/s3msgbroker");
}

void TestClientOverwriteFlag()
{
    // Test default behavior allows overwrite
    std::string firstTimestamp;
    std::string secondTimestamp;
    {
        firstTimestamp = std::to_string(NowAsTimestamp());
        _SendAndCheckProtocolClientMessageFlags(firstTimestamp, firstTimestamp, false);
        ASSERT_NE(firstTimestamp, "");
    }
    {
        secondTimestamp = std::to_string(NowAsTimestamp());
        _SendAndCheckProtocolClientMessageFlags(secondTimestamp, secondTimestamp, false);
        ASSERT_NE(secondTimestamp, "");
        ASSERT_NE(firstTimestamp, secondTimestamp);
    }

    // Test overwrites explicitly disallowed
    {
        std::string now = std::to_string(NowAsTimestamp());
        _SendAndCheckProtocolClientMessageFlags(now, secondTimestamp, true, false);
        ASSERT_NE(now, secondTimestamp);
    }

    // Test overwrites explicitly allowed
    {
        std::string now = std::to_string(NowAsTimestamp());
        _SendAndCheckProtocolClientMessageFlags(now, now, true, true);
        ASSERT_NE(now, secondTimestamp);
    }

    // Test that batch payload expansion doesn't change anything when used with a non-batch payload
    {
        std::string now = std::to_string(NowAsTimestamp());
        _SendAndCheckProtocolClientMessageFlags(now, now, true, true, true, true);
        ASSERT_NE(now, secondTimestamp);
    }
    {
        std::string now = std::to_string(NowAsTimestamp());
        _SendAndCheckProtocolClientMessageFlags(now, now, true, true, true, false);
        ASSERT_NE(now, secondTimestamp);
    }
}

void TestMacros(bool useClient)
{
    HRESULT hr = S_OK;

    ComPtr<IPayload> payload;
    nlohmann::json jObj;
    std::string ts = std::to_string(NowAsTimestamp());
    jObj["timestamp"] = ts;
    ASSERT_S(MessageBroker::CreatePayload(payload.AddressOf(), jObj.dump().c_str()));
    ASSERT_S(payload->SetTimestamp(3));
    ASSERT_S(payload->SetCorrelationId("artifacts"));

    if (useClient)
    {
        _sendProtocolClientMessage(payload, "panorama-sdk-v2-${c_id}", "test/s${timestamp}msgbroker");
    }
    else
    {
        _sendMessageBrokerMessage(payload, "panorama-sdk-v2-${c_id}", "test/s${timestamp}msgbroker");
    }
    
    _checkContentTimestampField(ts, "panorama-sdk-v2-artifacts", "test/s3msgbroker");
}

void TestBatchExpansion(bool useClient)
{
    HRESULT hr = S_OK;
    ComPtr<ICredentialProvider> creds = Panorama_Aws::DefaultCredentialProvider();
    ASSERT_TRUE(creds != nullptr);
    ComPtr<IProtocolClient> client;
    ASSERT_S(Panorama_Aws::S3ProtocolClient(client.AddressOf(), "us-west-2", creds));

    ComPtr<IBatchPayload> batchPayload;
    ASSERT_S(MessageBroker::CreateBatchPayload(batchPayload.AddressOf()));

    // Test empty batch with batch expansion
    {
        std::stringstream key;
        key << "test/s3msgbroker" << batchPayload->Id();
        if (useClient)
        {
            _sendProtocolClientMessage(batchPayload, "panorama-sdk-v2-artifacts", key.str().c_str(), true, true);
        }
        else
        {
            _sendMessageBrokerMessage(batchPayload, "panorama-sdk-v2-artifacts", key.str().c_str(), true, true);
        }

        // There are no sub-payloads to upload, so batch expansion with an empty payload does not upload anything.
        ComPtr<IPropertyDelegate> s3PropertyDelegate;
        ASSERT_F(CreateS3PropertyDelegate(s3PropertyDelegate.AddressOf(), "panorama-sdk-v2-artifacts", key.str().c_str(), "us-west-2", creds));
    }

    // Test empty batch without batch expansion (should upload the serialize report)
    {
        ASSERT_S(batchPayload->SetTimestamp(3));
        ASSERT_S(batchPayload->SetCorrelationId("artifacts"));
        if (useClient)
        {
            _sendProtocolClientMessage(batchPayload, "panorama-sdk-v2-${c_id}", "test/s${timestamp}msgbroker", true, false);
        }
        else
        {
            _sendMessageBrokerMessage(batchPayload, "panorama-sdk-v2-${c_id}", "test/s${timestamp}msgbroker", true, false);
        }

        ComPtr<IPropertyDelegate> s3PropertyDelegate;
        ASSERT_S(CreateS3PropertyDelegate(s3PropertyDelegate.AddressOf(), "panorama-sdk-v2-artifacts", "test/s3msgbroker", "us-west-2", creds));
        ComPtr<IProperty> id;
        ASSERT_S(s3PropertyDelegate->GetProperty(id.AddressOf(), "id"));
        ASSERT_FALSE(strcmp(id.QueryInterface<IStringProperty>()->Get(), batchPayload->Id()));

        ComPtr<IProperty> payload_count;
        ASSERT_S(s3PropertyDelegate->GetProperty(payload_count.AddressOf(), "payload_count"));
        ASSERT_EQ(payload_count.QueryInterface<IIntegerProperty>()->Get(), 0);
    }
    
    // Test batch with 2 payloads with batch expansion (should upload the payloads)
    std::string subPayloadContent = std::to_string(NowAsTimestamp());
    {
        ComPtr<IPayload> subPayload1;
        ComPtr<IPayload> subPayload2;
        nlohmann::json jSubPayloadContent;
        jSubPayloadContent["timestamp"] = subPayloadContent;
        ASSERT_S(MessageBroker::CreatePayload(subPayload1.AddressOf(), jSubPayloadContent.dump().c_str()));
        ASSERT_S(MessageBroker::CreatePayload(subPayload2.AddressOf(), jSubPayloadContent.dump().c_str()));
        ASSERT_S(subPayload1->SetCorrelationId("subpayload1"));
        ASSERT_S(subPayload2->SetCorrelationId("subpayload2"));
        ASSERT_S(batchPayload->AddPayload(subPayload1));
        ASSERT_S(batchPayload->AddPayload(subPayload2));
        if (useClient)
        {
            _sendProtocolClientMessage(batchPayload, "panorama-sdk-v2-artifacts", "test/s3msgbroker/${c_id}", true, true);
        }
        else
        {
            _sendMessageBrokerMessage(batchPayload, "panorama-sdk-v2-artifacts", "test/s3msgbroker/${c_id}", true, true);
        }
        _checkContentTimestampField(subPayloadContent, "panorama-sdk-v2-artifacts", "test/s3msgbroker/subpayload1");
        _checkContentTimestampField(subPayloadContent, "panorama-sdk-v2-artifacts", "test/s3msgbroker/subpayload2");
    }

    // Test batch with the same 2 payloads, plus another empty batch payload, with batch expansion
    ComPtr<IBatchPayload> subBatchPayload;
    {
        ASSERT_S(MessageBroker::CreateBatchPayload(subBatchPayload.AddressOf()));
        ASSERT_S(batchPayload->AddPayload(subBatchPayload));
        if (useClient)
        {
            _sendProtocolClientMessage(batchPayload, "panorama-sdk-v2-artifacts", "test/s3msgbroker/${c_id}", true, true);
        }
        else
        {
            _sendMessageBrokerMessage(batchPayload, "panorama-sdk-v2-artifacts", "test/s3msgbroker/${c_id}", true, true);
        }
        _checkContentTimestampField(subPayloadContent, "panorama-sdk-v2-artifacts", "test/s3msgbroker/subpayload1");
        _checkContentTimestampField(subPayloadContent, "panorama-sdk-v2-artifacts", "test/s3msgbroker/subpayload2");
    }

    // Test batch with the same 2 payloads, plus another batch with a payload, with batch expansion
    std::string subSubPayloadContent = std::to_string(NowAsTimestamp());
    {
        ComPtr<IPayload> subSubPayload3;
        nlohmann::json jSubSubPayloadContent;
        jSubSubPayloadContent["timestamp"] = subSubPayloadContent;
        ASSERT_S(MessageBroker::CreatePayload(subSubPayload3.AddressOf(), jSubSubPayloadContent.dump().c_str()));
        ASSERT_S(subSubPayload3->SetCorrelationId("subsubpayload3"));
        ASSERT_S(subBatchPayload->AddPayload(subSubPayload3));
        if (useClient)
        {
            _sendProtocolClientMessage(batchPayload, "panorama-sdk-v2-artifacts", "test/s3msgbroker/${c_id}", true, true);
        }
        else
        {
            _sendMessageBrokerMessage(batchPayload, "panorama-sdk-v2-artifacts", "test/s3msgbroker/${c_id}", true, true);
        }
        _checkContentTimestampField(subPayloadContent, "panorama-sdk-v2-artifacts", "test/s3msgbroker/subpayload1");
        _checkContentTimestampField(subPayloadContent, "panorama-sdk-v2-artifacts", "test/s3msgbroker/subpayload2");
        _checkContentTimestampField(subSubPayloadContent, "panorama-sdk-v2-artifacts", "test/s3msgbroker/subsubpayload3");
    }

    {
        // Test overwrite with batch expansion, with no macros (same place gets overwritten a lot)
        if (useClient)
        {
            _sendProtocolClientMessage(batchPayload, "panorama-sdk-v2-artifacts", "test/s3msgbroker", true, true);
        }
        else
        {
            _sendMessageBrokerMessage(batchPayload, "panorama-sdk-v2-artifacts", "test/s3msgbroker", true, true);
        }
        _checkContentTimestampField(subSubPayloadContent, "panorama-sdk-v2-artifacts", "test/s3msgbroker");

        // Create a property delegate now so we can check for any changes using the same delegate later
        ComPtr<IPropertyDelegate> delegate;
        ASSERT_S(CreateS3PropertyDelegate(delegate.AddressOf(), "panorama-sdk-v2-artifacts", "test/s3msgbroker", "us-west-2", creds));

        // Test no overwrite with batch expansion, with no macros
        if (useClient)
        {
            _sendProtocolClientMessage(batchPayload, "panorama-sdk-v2-artifacts", "test/s3msgbroker", false, true);
        }
        else
        {
            _sendMessageBrokerMessage(batchPayload, "panorama-sdk-v2-artifacts", "test/s3msgbroker", false, true);
        }

        // no update since last check means S_FALSE
        ComPtr<IPropertyCollection> changed_properties;
        EXPECT_EQ(delegate->Synchronize(changed_properties.AddressOf()), S_FALSE);
    }
}

void _sendAndCheckMessageBrokerMessageFlags(const std::string& messageBrokerConfig, const std::string& messageTimestamp, const std::string& expectedTimestamp)
{
    HRESULT hr = S_OK;

    ComPtr<ICredentialProvider> creds = Panorama_Aws::DefaultCredentialProvider();
    ASSERT_TRUE(creds != nullptr);

    ComPtr<IMessageBroker> broker;
    ASSERT_S(MessageBroker::Create(broker.AddressOf(), creds, messageBrokerConfig.c_str(), true));
    ASSERT_S(broker->Initialize());

    nlohmann::json jObj;
    jObj["timestamp"] = messageTimestamp;
    ComPtr<IPayload> payload;
    ASSERT_S(MessageBroker::CreatePayload(payload.AddressOf(), jObj.dump().c_str()));
    ASSERT_S(payload->SetCorrelationId("correlation-id"));
    ASSERT_S(broker->Publish("test_message", payload));

    _checkContentTimestampField(expectedTimestamp, "panorama-sdk-v2-artifacts", "test/s3protocolmsgbroker_correlation-id");
}

void TestMessageBrokerOverwriteFlag()
{
    HRESULT hr = S_OK;

    // Test default behavior allows overwrite
    std::string config = 
    "{                                                                                  "
    "    \"targets\": [                                                                 "
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
    "                    \"target_name\": \"test-s3\",                                  "
    "                    \"s3_message_options\": {                                      "
    "                        \"bucket\": \"panorama-sdk-v2-artifacts\",                 "
    "                        \"key\": \"test/s3protocolmsgbroker_${c_id}\"              "
    "                    }                                                              "
    "                }                                                                  "
    "            ]                                                                      "
    "        }                                                                          "
    "    ]                                                                              "
    "}                                                                                  ";
    std::string firstTimestamp = std::to_string(NowAsTimestamp());
    _sendAndCheckMessageBrokerMessageFlags(config, firstTimestamp, firstTimestamp);
    ASSERT_NE(firstTimestamp, "");
    std::string secondTimestamp = std::to_string(NowAsTimestamp());
    _sendAndCheckMessageBrokerMessageFlags(config, secondTimestamp, secondTimestamp);
    ASSERT_NE(secondTimestamp, "");
    ASSERT_NE(firstTimestamp, secondTimestamp);

    // Test overwrites explicitly disallowed
    config = 
    "{                                                                                  "
    "    \"targets\": [                                                                 "
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
    "                    \"target_name\": \"test-s3\",                                  "
    "                    \"s3_message_options\": {                                      "
    "                        \"bucket\": \"panorama-sdk-v2-artifacts\",                 "
    "                        \"key\": \"test/s3protocolmsgbroker_${c_id}\",             "
    "                        \"overwrite\": false                                       "
    "                    }                                                              "
    "                }                                                                  "
    "            ]                                                                      "
    "        }                                                                          "
    "    ]                                                                              "
    "}                                                                                  ";
    std::string now = std::to_string(NowAsTimestamp());
    _sendAndCheckMessageBrokerMessageFlags(config, now, secondTimestamp);
    ASSERT_NE(now, secondTimestamp);

    // Test overwrites explicitly allowed
    config = 
    "{                                                                                  "
    "    \"targets\": [                                                                 "
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
    "                    \"target_name\": \"test-s3\",                                  "
    "                    \"s3_message_options\": {                                      "
    "                        \"bucket\": \"panorama-sdk-v2-artifacts\",                 "
    "                        \"key\": \"test/s3protocolmsgbroker_${c_id}\",             "
    "                        \"overwrite\": true                                        "
    "                    }                                                              "
    "                }                                                                  "
    "            ]                                                                      "
    "        }                                                                          "
    "    ]                                                                              "
    "}                                                                                  ";
    now = std::to_string(NowAsTimestamp());
    _sendAndCheckMessageBrokerMessageFlags(config, now, now);
    ASSERT_NE(now, secondTimestamp);
}

TEST(AWSTests, S3ProtocolClientTests)
{
    TestClientOverwriteFlag();
    TestMacros(true);
    TestMacros(false);
    TestMessageBrokerOverwriteFlag();
    TestBatchExpansion(true);
    TestBatchExpansion(false);
}
