#include <thread>
#include <fstream>
#include <sstream>

#include <gtest/gtest.h>
#include <Panorama/apidefs.h>
#include <Panorama/comptr.h>
#include <Panorama/chrono.h>
#include <Panorama/buffer.h>
#include <Panorama/aws.h>
#include <Panorama/comptr.h>
#include <Panorama/app.h>
#include <Panorama/message_broker.h>
#include <Panorama/videocapture.h>
#include <core/message_broker/protocol_client_base.h>
#include <core/message_broker/message_id_variables.h>

#include <nlohmann/json.hpp>

#include <nlohmann/json.hpp>

#include <TestUtils.h>
#include "test_protocol_client.h"


using namespace Panorama;

void TestExpandMacros()
{
    HRESULT hr;

    ComPtr<IPayload> payload;
    ASSERT_S(MessageBroker::CreatePayload(payload.AddressOf(), "mypayload"));

    ASSERT_EQ(ExpandMacros("", payload), "");
    ASSERT_EQ(ExpandMacros(nullptr, payload), "");
    ASSERT_EQ(ExpandMacros("invalid payload", nullptr), "");
    ASSERT_EQ(ExpandMacros("", nullptr), "");
    ASSERT_EQ(ExpandMacros(nullptr, nullptr), "");

    ASSERT_EQ(ExpandMacros("no macro but looks sorta like one $id c_id {timestamp}", payload), "no macro but looks sorta like one $id c_id {timestamp}");

    {
        std::stringstream s;
        s << payload->Id() << " macro";
        ASSERT_EQ(ExpandMacros("${id} macro", payload), s.str());
    }

    payload->SetTimestamp(1234);
    ASSERT_EQ(ExpandMacros("${timestamp} macro", payload), "1234 macro");

    ASSERT_EQ(ExpandMacros("${c_id}", payload), "");
    ASSERT_EQ(ExpandMacros("${c_id} macro", payload), " macro");
    payload->SetCorrelationId("${timestamp}");
    ASSERT_EQ(ExpandMacros("${c_id} macro", payload), "${timestamp} macro");
    payload->SetCorrelationId("${id}");
    ASSERT_EQ(ExpandMacros("${c_id} macro", payload), "${id} macro");
    payload->SetCorrelationId("${c_id}");
    ASSERT_EQ(ExpandMacros("${c_id} macro", payload), "${c_id} macro");
    payload->SetCorrelationId("mycid");
    ASSERT_EQ(ExpandMacros("${c_id} macro", payload), "mycid macro");

    {
        std::stringstream s;
        s << payload->Id() << "mycid1234";
        ASSERT_EQ(ExpandMacros("${id}${c_id}${timestamp}", payload), s.str());
    }

    {
        std::stringstream s;
        s << "mycid" << payload->Id() << payload->Id() << "12341234mycidmycid";
        ASSERT_EQ(ExpandMacros("${c_id}${id}${id}${timestamp}${timestamp}${c_id}${c_id}", payload), s.str());
    }

    {
        payload->SetTimestamp(1);
        std::stringstream s;
        s << "mycid" << payload->Id() << payload->Id() << "11mycidmycid";
        ASSERT_EQ(ExpandMacros("${c_id}${id}${id}${timestamp}${timestamp}${c_id}${c_id}", payload), s.str());
    }

    {
        payload->SetCorrelationId("${c_id}");
        std::stringstream s;
        s << "${c_id}" << payload->Id() << payload->Id() << "11${c_id}${c_id}";
        ASSERT_EQ(ExpandMacros("${c_id}${id}${id}${timestamp}${timestamp}${c_id}${c_id}", payload), s.str());
    }

    {
        std::stringstream s;
        s << "1_test_file-" << payload->Id() << "-${c_id}_with_funny_${c_id}_1_haha";
        ASSERT_EQ(ExpandMacros("${timestamp}_test_file-${id}-${c_id}_with_funny_${c_id}_${timestamp}_haha", payload), s.str());
    }

    {
        payload->SetCorrelationId("my-correlation-id");
        std::stringstream s;
        s << "1_test_file-" << payload->Id() << "-my-correlation-id_again_" << payload->Id() << "_1_my-correlation-id";
        ASSERT_EQ(ExpandMacros("${timestamp}_test_file-${id}-${c_id}_again_${id}_${timestamp}_${c_id}", payload), s.str());
    }

    {
        ASSERT_EQ(ExpandMacros("${count}_foo", payload), "0_foo");
        ASSERT_EQ(ExpandMacros("${count}_foo", payload), "1_foo");
        ASSERT_EQ(ExpandMacros("${count}_foo", payload), "2_foo");
        ASSERT_EQ(ExpandMacros("${count}_bar", payload), "0_bar");
        ASSERT_EQ(ExpandMacros("${count}_bar", payload), "1_bar");
        ASSERT_EQ(ExpandMacros("${count}_bar", payload), "2_bar");
        ASSERT_EQ(ExpandMacros("${count}_foo", payload), "3_foo");
        ASSERT_EQ(ExpandMacros("${count}_foobar", payload), "0_foobar");
    }
}

void TestCreateMessageBroker()
{
    HRESULT hr = S_OK;

    {
        // Invalid configuration
        ComPtr<IMessageBroker> broker;
        
        // not valid json config
        ASSERT_F(MessageBroker::Create(nullptr, nullptr, "invalid json"));
        ASSERT_F(MessageBroker::Create(broker.AddressOf(), nullptr, "invalid json"));

        // set the default config to invalid json
        MessageBroker::SetDefaultConfig("invalid json");
        ASSERT_F(MessageBroker::Create(broker.AddressOf()));

        // setting MESSAGE_BROKER_CONFIG_FILE to non exsistent file
        MessageBroker::SetDefaultConfig(nullptr); // clear default config
        SetEnvVar("MESSAGE_BROKER_CONFIG_FILE", "file that doesn't exist");
        ASSERT_F(MessageBroker::Create(broker.AddressOf()));
    }

    {
        // Create the broker with the same configuration
        ComPtr<IMessageBroker> broker1, broker2;
        ASSERT_S(MessageBroker::Create(broker1.AddressOf(), nullptr, "[]"));
        ASSERT_S(MessageBroker::Create(broker2.AddressOf(), nullptr, "[]"));
        ASSERT_TRUE(broker1.Ptr() == broker2.Ptr());
    }

    {
        // Create the event broker with the same configuration but after going out of scope
        // No gurantee that pointers will be different, but at least excercise this path
        // Could possbily add a UUID to validate they are different
        {
            ComPtr<IMessageBroker> broker;
            ASSERT_S(MessageBroker::Create(broker.AddressOf(), nullptr, "{}"));
        }
        {
            ComPtr<IMessageBroker> broker;
            ASSERT_S(MessageBroker::Create(broker.AddressOf(), nullptr, "{}"));
        }
    }

    {
        // Load from default config
        MessageBroker::SetDefaultConfig("{}");
        ComPtr<IMessageBroker> broker;
        ASSERT_S(MessageBroker::Create(broker.AddressOf()));
        MessageBroker::SetDefaultConfig(nullptr);
    }
}

void TestCreateBufferPayload()
{
    HRESULT hr = S_OK;
    ComPtr<IPayload> payload1;
    ComPtr<IPayload> payload2;
    ASSERT_F(CreatePayloadFromString(nullptr, "contents"));
    ASSERT_F(CreatePayloadFromString(nullptr, nullptr));
    ASSERT_F(CreatePayloadFromString(payload1.AddressOf(), nullptr));
    ASSERT_S(CreatePayloadFromString(payload1.AddressOf(), ""));
    ASSERT_S(CreatePayloadFromString(payload2.AddressOf(), "contents"));

    ComPtr<IPayload> payload3;
    ComPtr<IBuffer> buffer;
    ASSERT_S(CreateBufferFromString(buffer.AddressOf(), "contents"));

    ASSERT_F(CreatePayloadFromBuffer(nullptr, buffer));
    ASSERT_F(CreatePayloadFromBuffer(nullptr, nullptr));
    ASSERT_F(CreatePayloadFromBuffer(payload3.AddressOf(), nullptr));
    ASSERT_S(CreatePayloadFromBuffer(payload3.AddressOf(), buffer));

    ComPtr<IPayload> payload4;
    ComPtr<IPayload> payload5;
    ASSERT_F(MessageBroker::CreatePayload(nullptr, (const char*) nullptr));
    ASSERT_F(MessageBroker::CreatePayload(nullptr, (const char*) ""));
    ASSERT_F(MessageBroker::CreatePayload(payload4.AddressOf(), (const char*) nullptr));
    ASSERT_S(MessageBroker::CreatePayload(payload4.AddressOf(), "teststring"));
    ASSERT_S(MessageBroker::CreatePayload(payload5.AddressOf(), ""));

    ComPtr<IPayload> payload6;
    ASSERT_F(MessageBroker::CreatePayload(nullptr, buffer));
    ASSERT_F(MessageBroker::CreatePayload(nullptr, (IBuffer*) nullptr));
    ASSERT_F(MessageBroker::CreatePayload(payload6.AddressOf(), (IBuffer*) nullptr));
    ASSERT_S(MessageBroker::CreatePayload(payload6.AddressOf(), buffer));
}

void TestCreateBatchPayload()
{
    HRESULT hr = S_OK;
    ComPtr<IBatchPayload> batchPayload;
    ASSERT_F(CreateEmptyBatchPayload(nullptr));
    ASSERT_S(CreateEmptyBatchPayload(batchPayload.AddressOf()));
    
    ComPtr<IBatchPayload> batchPayload1;
    ASSERT_F(MessageBroker::CreateBatchPayload(nullptr));
    ASSERT_S(MessageBroker::CreateBatchPayload(batchPayload1.AddressOf()));
}

void TestCreateVideoPayload()
{
    HRESULT hr = S_OK;

    ComPtr<IVideoClip> clip;
    ASSERT_S(Panorama::CreateFFmpegVideoClip(clip.AddressOf(), 640, 480, Encoding::H264, ContainerFormat::MP4));

    ComPtr<IVideoPayload> videoPayload;
    ASSERT_F(VideoCapture::VideoPayload(nullptr, nullptr));
    ASSERT_F(VideoCapture::VideoPayload(nullptr, clip));
    ASSERT_F(VideoCapture::VideoPayload(videoPayload.AddressOf(), nullptr));
    ASSERT_S(VideoCapture::VideoPayload(videoPayload.AddressOf(), clip));

    ComPtr<IVideoPayload> videoPayload1;
    ASSERT_F(CreateVideoPayloadFromVideoClip(nullptr, nullptr));
    ASSERT_F(CreateVideoPayloadFromVideoClip(nullptr, clip));
    ASSERT_F(CreateVideoPayloadFromVideoClip(videoPayload1.AddressOf(), nullptr));
    ASSERT_S(CreateVideoPayloadFromVideoClip(videoPayload1.AddressOf(), clip));
}

void TestId()
{
    HRESULT hr = S_OK;

    ComPtr<IMessageBroker> broker;
    ASSERT_S(MessageBroker::Create(broker.AddressOf(), nullptr, "{}"));

    ComPtr<IPayload> payload;
    ComPtr<IBatchPayload> batchPayload;
    ComPtr<IVideoPayload> videoPayload;
    ASSERT_S(MessageBroker::CreatePayload(payload.AddressOf(), "contents"));
    ASSERT_S(MessageBroker::CreateBatchPayload(batchPayload.AddressOf()));
    ComPtr<IVideoClip> clip;
    ASSERT_S(Panorama::CreateFFmpegVideoClip(clip.AddressOf(), 640, 480, Encoding::H264, ContainerFormat::MP4));
    ASSERT_S(VideoCapture::VideoPayload(videoPayload.AddressOf(), clip));

    AutoResetEvent message_received;
    std::string lastId;
    ASSERT_S(broker->Subscribe("id", [&](IPayload* message)
    {
        ASSERT_NE(message->Id(), nullptr);
        ASSERT_EQ(strlen(message->Id()), 36);
        ASSERT_NE(strcmp(lastId.c_str(), message->Id()), 0);
        lastId = message->Id();
        message_received.Set();
    }));
    int32_t token1 = hr;
    ASSERT_S(broker->Publish("id", payload));
    ASSERT_TRUE(message_received.WaitFor(0));
    ASSERT_S(broker->Publish("id", batchPayload));
    ASSERT_TRUE(message_received.WaitFor(0));
    ASSERT_S(broker->Publish("id", videoPayload));
    ASSERT_TRUE(message_received.WaitFor(0));
    ASSERT_S(broker->Unsubscribe(token1));
}

void TestCorrelationId()
{
    HRESULT hr = S_OK;

    ComPtr<IMessageBroker> broker;
    ASSERT_S(MessageBroker::Create(broker.AddressOf(), nullptr, "{}"));

    ComPtr<IPayload> payload;
    ComPtr<IBatchPayload> batchPayload;
    ComPtr<IVideoPayload> videoPayload;
    ASSERT_S(MessageBroker::CreatePayload(payload.AddressOf(), "contents"));
    ASSERT_S(MessageBroker::CreateBatchPayload(batchPayload.AddressOf()));
    ComPtr<IVideoClip> clip;
    ASSERT_S(Panorama::CreateFFmpegVideoClip(clip.AddressOf(), 640, 480, Encoding::H264, ContainerFormat::MP4));
    ASSERT_S(VideoCapture::VideoPayload(videoPayload.AddressOf(), clip));

    AutoResetEvent message_received;
    ASSERT_S(broker->Subscribe("default_cid", [&](IPayload* message)
    {
        // Verify default correlation id
        ASSERT_EQ(strcmp(message->CorrelationId(), ""), 0);
        message_received.Set();
    }));
    int32_t token1 = hr;
    ASSERT_S(broker->Publish("default_cid", payload));
    ASSERT_TRUE(message_received.WaitFor(0));
    ASSERT_S(broker->Publish("default_cid", batchPayload));
    ASSERT_TRUE(message_received.WaitFor(0));
    ASSERT_S(broker->Publish("default_cid", videoPayload));
    ASSERT_TRUE(message_received.WaitFor(0));

    ASSERT_S(payload->SetCorrelationId("my-cid"));
    ASSERT_S(batchPayload->SetCorrelationId("my-cid"));
    ASSERT_S(videoPayload->SetCorrelationId("my-cid"));
    ASSERT_F(payload->SetCorrelationId(nullptr));
    ASSERT_F(batchPayload->SetCorrelationId(nullptr));
    ASSERT_F(videoPayload->SetCorrelationId(nullptr));
    ASSERT_S(broker->Subscribe("with_cid", [&](IPayload* message)
    {
        // Verify correlation id
        ASSERT_EQ(strcmp(message->CorrelationId(), "my-cid"), 0);
        message_received.Set();
    }));
    int32_t token2 = hr;
    ASSERT_S(broker->Publish("with_cid", payload));
    ASSERT_TRUE(message_received.WaitFor(0));
    ASSERT_S(broker->Publish("with_cid", batchPayload));
    ASSERT_TRUE(message_received.WaitFor(0));
    ASSERT_S(broker->Publish("with_cid", videoPayload));
    ASSERT_TRUE(message_received.WaitFor(0));

    ASSERT_S(broker->Unsubscribe(token1));
    ASSERT_S(broker->Unsubscribe(token2));
}

void TestTimestamp()
{
    HRESULT hr = S_OK;

    ComPtr<IMessageBroker> broker;
    ASSERT_S(MessageBroker::Create(broker.AddressOf(), nullptr, "{}"));

    ComPtr<IPayload> payload;
    ComPtr<IBatchPayload> batchPayload;
    ComPtr<IVideoPayload> videoPayload;
    auto beforeTs = NowAsTimestamp();
    ASSERT_S(MessageBroker::CreatePayload(payload.AddressOf(), "contents"));
    ASSERT_S(MessageBroker::CreateBatchPayload(batchPayload.AddressOf()));
    ComPtr<IVideoClip> clip;
    ASSERT_S(Panorama::CreateFFmpegVideoClip(clip.AddressOf(), 640, 480, Encoding::H264, ContainerFormat::MP4));
    ASSERT_S(VideoCapture::VideoPayload(videoPayload.AddressOf(), clip));
    auto afterTs = NowAsTimestamp();

    AutoResetEvent message_received;
    ASSERT_S(broker->Subscribe("default_ts", [&](IPayload* message)
    {
        // Verify default timestamp
        ASSERT_TRUE(message->Timestamp() >= beforeTs);
        ASSERT_TRUE(message->Timestamp() <= afterTs);
        message_received.Set();
    }));
    int32_t token1 = hr;
    ASSERT_S(broker->Publish("default_ts", payload));
    ASSERT_TRUE(message_received.WaitFor(0));
    ASSERT_S(broker->Publish("default_ts", batchPayload));
    ASSERT_TRUE(message_received.WaitFor(0));
    ASSERT_S(broker->Publish("default_ts", videoPayload));
    ASSERT_TRUE(message_received.WaitFor(0));

    payload->SetTimestamp(123);
    batchPayload->SetTimestamp(123);
    videoPayload->SetTimestamp(123);
    ASSERT_S(broker->Subscribe("set_ts", [&](IPayload* message)
    {
        // Verify timestamp
        ASSERT_EQ(message->Timestamp(), 123);
        message_received.Set();
    }));
    int32_t token2 = hr;
    ASSERT_S(broker->Publish("set_ts", payload));
    ASSERT_TRUE(message_received.WaitFor(0));
    ASSERT_S(broker->Publish("set_ts", batchPayload));
    ASSERT_TRUE(message_received.WaitFor(0));
    ASSERT_S(broker->Publish("set_ts", videoPayload));
    ASSERT_TRUE(message_received.WaitFor(0));

    ASSERT_S(broker->Unsubscribe(token1));
    ASSERT_S(broker->Unsubscribe(token2));
}

void LocalPublishSubscribe()
{
    HRESULT hr = S_OK;

    ComPtr<IMessageBroker> broker;
    ASSERT_S(MessageBroker::Create(broker.AddressOf(), nullptr, "{}"));

    AutoResetEvent message_received_1, message_received_2, message_received_3;
    ASSERT_S(broker->Subscribe("test_message_1", [&](IPayload* message)
    {
        ASSERT_TRUE(strcmp(message->SerializeAsString(), "contents") == 0);
        message_received_1.Set();
    }));
    int32_t token1 = hr;

    ASSERT_S(broker->Subscribe("test_message_2", [&](IPayload* message)
    {
        ASSERT_TRUE(strcmp(message->SerializeAsString(), "contents") == 0);
        message_received_2.Set();
    }));
    int32_t token2 = hr;

    ASSERT_S(broker->Subscribe("test_message_3", [&](IPayload* message)
    {
        ASSERT_TRUE(strcmp(message->SerializeAsString(), "contents") == 0);
        message_received_3.Set();
    }));
    int32_t token3 = hr;

    ASSERT_NE(token1, token2);
    ASSERT_NE(token1, token3);
    ASSERT_NE(token2, token3);

    ComPtr<IPayload> payload;
    ASSERT_S(MessageBroker::CreatePayload(payload.AddressOf(), "contents"));
    ASSERT_S(broker->Publish("test_message_1", payload));
    ASSERT_TRUE(message_received_1.WaitFor(0));
    ASSERT_FALSE(message_received_2.WaitFor(0));
    ASSERT_FALSE(message_received_3.WaitFor(0));

    ASSERT_S(broker->Publish("test_message_2", payload));
    ASSERT_FALSE(message_received_1.WaitFor(0));
    ASSERT_TRUE(message_received_2.WaitFor(0));
    ASSERT_FALSE(message_received_3.WaitFor(0));

    ASSERT_S(broker->Publish("test_message_3", payload));
    ASSERT_FALSE(message_received_1.WaitFor(0));
    ASSERT_FALSE(message_received_2.WaitFor(0));
    ASSERT_TRUE(message_received_3.WaitFor(0));

    ASSERT_S(broker->Publish("test_message_4", payload));
    ASSERT_FALSE(message_received_1.WaitFor(0));
    ASSERT_FALSE(message_received_2.WaitFor(0));
    ASSERT_FALSE(message_received_3.WaitFor(0));

    // unsubscribe from test_message_2
    // validate other subscriptions still get callbacks
    broker->Unsubscribe(token2);
    ASSERT_S(broker->Publish("test_message_1", payload));
    ASSERT_TRUE(message_received_1.WaitFor(0));
    ASSERT_FALSE(message_received_2.WaitFor(0));
    ASSERT_FALSE(message_received_3.WaitFor(0));

    ASSERT_S(broker->Publish("test_message_2", payload));
    ASSERT_FALSE(message_received_1.WaitFor(0));
    ASSERT_FALSE(message_received_2.WaitFor(0));
    ASSERT_FALSE(message_received_3.WaitFor(0));
    
    ASSERT_S(broker->Publish("test_message_3", payload));
    ASSERT_FALSE(message_received_1.WaitFor(0));
    ASSERT_FALSE(message_received_2.WaitFor(0));
    ASSERT_TRUE(message_received_3.WaitFor(0));

    // unsubscribe from rest
    broker->Unsubscribe(token1);
    broker->Unsubscribe(token3);

    ASSERT_S(broker->Publish("test_message_1", payload));
    ASSERT_FALSE(message_received_1.WaitFor(0));

    ASSERT_S(broker->Publish("test_message_3", payload));
    ASSERT_FALSE(message_received_3.WaitFor(0));
    
    // resubscribe
    ASSERT_S(broker->Subscribe("test_message_2", [&](IPayload* message)
    {
        ASSERT_TRUE(strcmp(message->SerializeAsString(), "contents") == 0);
        message_received_2.Set();
    }));
    token2 = hr;

    ASSERT_S(broker->Publish("test_message_2", payload));
    ASSERT_TRUE(message_received_2.WaitFor(0));

    // multiple subscriptions
    AutoResetEvent message_received_4;
    ASSERT_S(broker->Subscribe("test_message_2", [&](IPayload* message)
    {
        ASSERT_TRUE(strcmp(message->SerializeAsString(), "contents") == 0);
        message_received_4.Set();
    }));
    int32_t token4 = hr;

    ASSERT_S(broker->Publish("test_message_2", payload));
    ASSERT_TRUE(message_received_2.WaitFor(0));
    ASSERT_TRUE(message_received_4.WaitFor(0));

    // unsubscribe from sub-id with multiple subscriptoins
    broker->Unsubscribe(token2);
    ASSERT_S(broker->Publish("test_message_2", payload));
    ASSERT_FALSE(message_received_2.WaitFor(0));
    ASSERT_TRUE(message_received_4.WaitFor(0));
}

void TestBufferPayload()
{
    HRESULT hr = S_OK;
    ComPtr<IMessageBroker> broker;
    ASSERT_S(MessageBroker::Create(broker.AddressOf(), nullptr, "{}"));

    // Test serialize, serialize as string, since not tested elsewhere
    AutoResetEvent receivedEvent;
    ASSERT_S(broker->Subscribe("buffer_message", [&](IPayload* message)
    {
        ASSERT_F(message->Serialize(nullptr));
        ComPtr<IBuffer> serializeBuffer;
        ASSERT_S(message->Serialize(serializeBuffer.AddressOf()));
        ASSERT_EQ(strcmp(serializeBuffer->AsString(), message->SerializeAsString()), 0);
        ASSERT_EQ(strcmp(message->SerializeAsString(), "contents"), 0);
        receivedEvent.Set();
    }));
    int32_t token = hr;

    ComPtr<IPayload> payload1;
    ASSERT_S(CreatePayloadFromString(payload1.AddressOf(), "contents"));
    ASSERT_S(broker->Publish("buffer_message", payload1));
    ASSERT_TRUE(receivedEvent.WaitFor(0));

    ComPtr<IPayload> payload2;
    ComPtr<IBuffer> buffer;
    ASSERT_S(CreateBufferFromString(buffer.AddressOf(), "contents"));
    ASSERT_S(CreatePayloadFromBuffer(payload2.AddressOf(), buffer));
    ASSERT_S(broker->Publish("buffer_message", payload2));

    ASSERT_S(broker->Unsubscribe(token));
}

void _batchPayloadHelper(ComPtr<IBatchPayload> batch, std::string payloadId1, std::string payloadId2)
{
    HRESULT hr;

    ASSERT_NE(batch, nullptr);

    ASSERT_EQ(batch->Count(), 2);
    ComPtr<IPayload> payload1;
    ASSERT_EQ(batch->Payload(payload1.AddressOf(), 2), E_OUTOFRANGE);
    ASSERT_EQ(batch->Payload(payload1.AddressOf(), 0), S_OK);
    ASSERT_TRUE(strcmp(payload1->SerializeAsString(), "contents1") == 0);

    ComPtr<IPayload> theSamePayload1;
    ASSERT_EQ(batch->Payload(theSamePayload1.AddressOf(), "not_the_right_id"), E_NOT_FOUND);
    ASSERT_EQ(batch->Payload(theSamePayload1.AddressOf(), payloadId1.c_str()), S_OK);
    ASSERT_TRUE(strcmp(theSamePayload1->SerializeAsString(), "contents1") == 0);

    ComPtr<IPayload> payload2;
    ASSERT_EQ(batch->Payload(payload2.AddressOf(), 1), S_OK);
    ASSERT_TRUE(strcmp(payload2->SerializeAsString(), "contents2") == 0);

    ComPtr<IPayload> theSamePayload2;
    ASSERT_EQ(batch->Payload(theSamePayload2.AddressOf(), payloadId2.c_str()), S_OK);
    ASSERT_TRUE(strcmp(theSamePayload2->SerializeAsString(), "contents2") == 0);

    ComPtr<IBuffer> serializeBuffer;
    ASSERT_S(batch->Serialize(serializeBuffer.AddressOf()));
    ASSERT_EQ(strcmp(serializeBuffer->AsString(), batch->SerializeAsString()), 0);
    ASSERT_EQ(nlohmann::json::accept(serializeBuffer->AsString()), true);
    nlohmann::json serialized = nlohmann::json::parse(serializeBuffer->AsString());
    ASSERT_EQ(serialized.contains("timestamp"), true);
    ASSERT_EQ(serialized.contains("id"), true);
    ASSERT_EQ(serialized.contains("correlation_id"), true);
    ASSERT_EQ(serialized["payload_count"], 2);
    ASSERT_EQ(serialized["payloads"][0].contains("timestamp"), true);
    ASSERT_EQ(serialized["payloads"][0]["id"], payloadId1);
    ASSERT_EQ(serialized["payloads"][0].contains("correlation_id"), true);
    ASSERT_EQ(serialized["payloads"][1].contains("timestamp"), true);
    ASSERT_EQ(serialized["payloads"][1]["id"], payloadId2);
    ASSERT_EQ(serialized["payloads"][1].contains("correlation_id"), true);
}

void TestBatchPayload()
{
    HRESULT hr = S_OK;

    ComPtr<IMessageBroker> broker;
    ASSERT_S(MessageBroker::Create(broker.AddressOf(), nullptr, "{}"));

    // Test empty batch
    AutoResetEvent emptyBatchReceivedEvent;
    ASSERT_S(broker->Subscribe("empty_batch_message", [&](IPayload* message)
    {
        HRESULT hr = S_OK;

        ComPtr<IBatchPayload> batch = ComPtr<IPayload>(message).QueryInterface<IBatchPayload>();
        ASSERT_NE(batch, nullptr);

        ASSERT_EQ(batch->Count(), 0);
        ComPtr<IPayload> dummy;
        ASSERT_EQ(batch->Payload(dummy.AddressOf(), 0), E_OUTOFRANGE);
        ASSERT_EQ(batch->Payload(nullptr, 0), E_POINTER);
        ASSERT_EQ(batch->Payload(nullptr, 1), E_POINTER);
        ASSERT_EQ(batch->Payload(dummy.AddressOf(), -1), E_OUTOFRANGE);
        ASSERT_EQ(batch->Payload(dummy.AddressOf(), 1), E_OUTOFRANGE);
        ASSERT_EQ(batch->Payload(dummy.AddressOf(), nullptr), E_INVALIDARG);
        ASSERT_EQ(batch->Payload(nullptr, nullptr), E_POINTER);
        ASSERT_EQ(batch->Payload(dummy.AddressOf(), "not_the_right_id"), E_NOT_FOUND);
        ASSERT_EQ(batch->Payload(nullptr, "not_the_right_id"), E_POINTER);

        ComPtr<IBuffer> serializeBuffer;
        ASSERT_EQ(batch->Serialize(nullptr), E_POINTER);
        ASSERT_S(batch->Serialize(serializeBuffer.AddressOf()));
        ASSERT_EQ(strcmp(serializeBuffer->AsString(), batch->SerializeAsString()), 0);
        ASSERT_EQ(nlohmann::json::accept(serializeBuffer->AsString()), true);
        nlohmann::json serialized = nlohmann::json::parse(serializeBuffer->AsString());
        ASSERT_EQ(serialized.contains("timestamp"), true);
        ASSERT_EQ(serialized.contains("id"), true);
        ASSERT_EQ(serialized.contains("correlation_id"), true);
        ASSERT_EQ(serialized.contains("payload_count"), true);
        ASSERT_EQ(serialized.contains("payloads"), true);
        ASSERT_EQ(serialized["payload_count"], 0);
        ASSERT_EQ(serialized["payloads"], nlohmann::json::array());

        emptyBatchReceivedEvent.Set();
    }));
    int32_t emptyBatchToken = hr;
    ComPtr<IBatchPayload> emptyBatchPayload;
    ASSERT_S(MessageBroker::CreateBatchPayload(emptyBatchPayload.AddressOf()));
    ASSERT_EQ(emptyBatchPayload->AddPayload(nullptr), E_INVALIDARG);
    ASSERT_S(broker->Publish("empty_batch_message", emptyBatchPayload));
    ASSERT_TRUE(emptyBatchReceivedEvent.WaitFor(0));

    // Test single batch
    ComPtr<IBatchPayload> batchPayload;
    std::string payloadId1;
    ASSERT_S(MessageBroker::CreateBatchPayload(batchPayload.AddressOf()));
    {
        ComPtr<IPayload> payload;
        ASSERT_S(MessageBroker::CreatePayload(payload.AddressOf(), "contents1"));
        payloadId1 = payload->Id();
        ASSERT_S(batchPayload->AddPayload(payload));
        ASSERT_EQ(batchPayload->AddPayload(batchPayload), E_INVALIDARG);
    }

    AutoResetEvent singleBatchReceivedEvent;
    ASSERT_S(broker->Subscribe("single_batch_message", [&](IPayload* message)
    {
        HRESULT hr = S_OK;

        ComPtr<IBatchPayload> batch = ComPtr<IPayload>(message).QueryInterface<IBatchPayload>();
        ASSERT_NE(batch, nullptr);

        ASSERT_EQ(batch->Count(), 1);
        ComPtr<IPayload> payload;
        ASSERT_EQ(batch->Payload(payload.AddressOf(), 1), E_OUTOFRANGE);
        ASSERT_EQ(batch->Payload(payload.AddressOf(), -1), E_OUTOFRANGE);
        ASSERT_EQ(batch->Payload(nullptr, 0), E_POINTER);
        ASSERT_EQ(batch->Payload(payload.AddressOf(), 0), S_OK);
        ASSERT_TRUE(strcmp(payload->SerializeAsString(), "contents1") == 0);

        ComPtr<IPayload> theSamePayload;
        ASSERT_EQ(batch->Payload(nullptr, nullptr), E_POINTER);
        ASSERT_EQ(batch->Payload(nullptr, "not_the_right_id"), E_POINTER);
        ASSERT_EQ(batch->Payload(nullptr, payloadId1.c_str()), E_POINTER);
        ASSERT_EQ(batch->Payload(theSamePayload.AddressOf(), "not_the_right_id"), E_NOT_FOUND);
        ASSERT_EQ(batch->Payload(theSamePayload.AddressOf(), nullptr), E_INVALIDARG);
        ASSERT_EQ(batch->Payload(theSamePayload.AddressOf(), payloadId1.c_str()), S_OK);
        ASSERT_TRUE(strcmp(theSamePayload->SerializeAsString(), "contents1") == 0);

        ComPtr<IBuffer> serializeBuffer;
        ASSERT_S(batch->Serialize(serializeBuffer.AddressOf()));
        ASSERT_EQ(strcmp(serializeBuffer->AsString(), batch->SerializeAsString()), 0);
        ASSERT_EQ(nlohmann::json::accept(serializeBuffer->AsString()), true);
        nlohmann::json serialized = nlohmann::json::parse(serializeBuffer->AsString());
        ASSERT_EQ(serialized.contains("timestamp"), true);
        ASSERT_EQ(serialized.contains("id"), true);
        ASSERT_EQ(serialized.contains("correlation_id"), true);
        ASSERT_EQ(serialized["payload_count"], 1);
        ASSERT_EQ(serialized["payloads"][0].contains("timestamp"), true);
        ASSERT_EQ(serialized["payloads"][0]["id"], payloadId1);
        ASSERT_EQ(serialized["payloads"][0].contains("correlation_id"), true);

        singleBatchReceivedEvent.Set();
    }));
    int32_t singleBatchToken = hr;
    ASSERT_S(broker->Publish("single_batch_message", batchPayload));
    ASSERT_TRUE(singleBatchReceivedEvent.WaitFor(0));

    ComPtr<IPayload> payload2;
    ASSERT_S(MessageBroker::CreatePayload(payload2.AddressOf(), "contents2"));
    std::string payloadId2 = payload2->Id();
    ASSERT_S(batchPayload->AddPayload(payload2));

    AutoResetEvent multiBatchReceivedEvent;
    ASSERT_S(broker->Subscribe("multi_batch_message", [&](IPayload* message)
    {
        HRESULT hr = S_OK;

        ComPtr<IBatchPayload> batch = ComPtr<IPayload>(message).QueryInterface<IBatchPayload>();
        _batchPayloadHelper(batch, payloadId1, payloadId2);
        multiBatchReceivedEvent.Set();
    }));
    int32_t multiBatchToken = hr;
    ASSERT_S(broker->Publish("multi_batch_message", batchPayload));
    ASSERT_TRUE(multiBatchReceivedEvent.WaitFor(0));
    ASSERT_FALSE(singleBatchReceivedEvent.WaitFor(0));

    ComPtr<IBatchPayload> batchPayloadWithBatch;
    ASSERT_S(MessageBroker::CreateBatchPayload(batchPayloadWithBatch.AddressOf()));
    ASSERT_S(batchPayloadWithBatch->AddPayload(batchPayload));
    AutoResetEvent recursiveBatchReceivedEvent;
    ASSERT_S(broker->Subscribe("recursive_batch_message", [&](IPayload* message)
    {
        HRESULT hr = S_OK;

        ComPtr<IBatchPayload> batch = ComPtr<IPayload>(message).QueryInterface<IBatchPayload>();
        ASSERT_NE(batch, nullptr);

        ASSERT_EQ(batch->Count(), 1);
        
        ComPtr<IPayload> payload;
        ASSERT_S(batch->Payload(payload.AddressOf(), 0));

        ComPtr<IBatchPayload> batchInBatch = payload.QueryInterface<IBatchPayload>();
        _batchPayloadHelper(batchInBatch, payloadId1, payloadId2);
        recursiveBatchReceivedEvent.Set();
    }));
    int32_t recursiveBatchToken = hr;
    ASSERT_S(broker->Publish("recursive_batch_message", batchPayloadWithBatch));
    ASSERT_TRUE(recursiveBatchReceivedEvent.WaitFor(0));

    ASSERT_S(broker->Unsubscribe(emptyBatchToken));
    ASSERT_S(broker->Unsubscribe(singleBatchToken));
    ASSERT_S(broker->Unsubscribe(multiBatchToken));
    ASSERT_S(broker->Unsubscribe(recursiveBatchToken));
}

void TestVideoPayload()
{
    // Test serialize, serialize as string, serialize video clip, additional metadata, since not tested elsewhere
    HRESULT hr = S_OK;
    ComPtr<IMessageBroker> broker;
    ASSERT_S(MessageBroker::Create(broker.AddressOf(), nullptr, "{}"));

    AutoResetEvent receivedEvent;
    ASSERT_S(broker->Subscribe("default_video_message", [&](IPayload* message)
    {
        ComPtr<IVideoPayload> video = ComPtr<IPayload>(message).QueryInterface<IVideoPayload>();
        ASSERT_NE(video, nullptr);
        ASSERT_F(video->Serialize(nullptr));
        ComPtr<IBuffer> serializeBuffer;
        ASSERT_S(video->Serialize(serializeBuffer.AddressOf()));
        ASSERT_EQ(strcmp(serializeBuffer->AsString(), video->SerializeAsString()), 0);
        ASSERT_EQ(strcmp(video->SerializeAsString(), "FFMpegVideoClip does not implement AsString()"), 0);
        ASSERT_EQ(video->Duration(), 0);

        ASSERT_NE(serializeBuffer.QueryInterface<IVideoClip>(), nullptr);

        ComPtr<IVideoClip> videoBuffer;
        ASSERT_S(video->SerializeVideoClip(videoBuffer.AddressOf()));
        ASSERT_EQ(videoBuffer->Duration(), video->Duration());
        ASSERT_EQ(videoBuffer->Data(), serializeBuffer->Data());

        receivedEvent.Set();
    }));
    int32_t token = hr;

    ComPtr<IVideoClip> clip;
    ASSERT_S(Panorama::CreateFFmpegVideoClip(clip.AddressOf(), 640, 480, Encoding::H264, ContainerFormat::MP4));
    ComPtr<IVideoPayload> videoPayload;
    ASSERT_S(VideoCapture::VideoPayload(videoPayload.AddressOf(), clip));
    
    ASSERT_S(broker->Publish("default_video_message", videoPayload));
    ASSERT_TRUE(receivedEvent.WaitFor(0));
    ASSERT_S(broker->Unsubscribe(token));
}

void TestCustomProtocol()
{
    HRESULT hr = S_OK;
    std::string config = 
        "{                                                                  "
        "    \"targets\": [                                                 "
        "        {                                                          "
        "            \"protocol\": \"test_protocol\",                       "
        "            \"name\": \"custom_1\",                                "
        "            \"test_protocol_options\": {                           "
        "            },                                                     "
        "            \"test_protocol_subscriptions\": [                     "
        "                {                                                  "
        "                    \"subscription_id\": \"test_subscription_1\",  "
        "                    \"parameter\": \"hello\"                       "
        "                },                                                 "
        "                {                                                  "
        "                    \"subscription_id\": \"test_subscription_2\",  "
        "                    \"parameter\": \"world\"                       "
        "                }                                                  "
        "            ]                                                      "
        "        }                                                          "
        "    ],                                                             "
        "    \"pipes\": [                                                   "
        "        {                                                          "
        "            \"message_id\": \"test_message\",                      "
        "            \"destinations\": [                                    "
        "                {                                                  "
        "                    \"target_name\": \"custom_1\",                 "
        "                    \"test_protocol_message_options\": {           "
        "                        \"parameter\": \"foobar\"                  "
        "                    }                                              "
        "                }                                                  "
        "            ]                                                      "
        "        }                                                          "
        "    ]                                                              "
        "}                                                                  ";

    ComPtr<IMessageBroker> broker;
    ASSERT_S(MessageBroker::Create(broker.AddressOf(), nullptr, config.c_str()));
    
    // Trying to initialize before target factory for test_protocol was added, should fail
    ASSERT_F(broker->Initialize());

    // Add the factory
    AutoResetEvent message_published;
    ComPtr<TestProtocolFactory> factory;
    ASSERT_S(TestProtocolFactory::Create(factory.AddressOf(), [&](ITestMessage* payload)
    {
        ComPtr<IBuffer> buffer;
        ASSERT_S(payload->GetPayload()->Serialize(buffer.AddressOf()));

        ASSERT_TRUE(strcmp(payload->Parameter(), "foobar") == 0);
        ASSERT_TRUE(strcmp(buffer->AsString(), "contents") == 0);
        message_published.Set();
    }));

    ASSERT_S(broker->AddProtocolFactory(factory));
    ASSERT_S(broker->Initialize());

    // publish a message with an unconfigured message-id
    // This should return success, even though nothing is handling it
    ASSERT_S(broker->Publish("non_configured_message_id", "contents"));

    // publish a message
    ASSERT_S(broker->Publish("test_message", "contents"));
    ASSERT_TRUE(message_published.WaitFor(0));

    // async publish a message with a local subscription to that message
    AutoResetEvent local_subscription_called;
    ASSERT_S(broker->Subscribe("test_message", [&](IPayload* data)
    {
        local_subscription_called.Set();
    }));
    int32_t token3 = hr;

    AutoResetEvent loop_published_cb;
    AutoResetEvent test_published_cb;
    ASSERT_S(broker->PublishAsync("test_message", "contents", [&](const char* publisher, const char* message_id, IPayload* message, bool successful)
    {
        if(strcmp(publisher, "loopback") == 0)
        {
            loop_published_cb.Set();
        }
        else if(strcmp(publisher, "test_protocol") == 0)
        {
            test_published_cb.Set();
        }
        else
        {
            ASSERT_TRUE(false);
        }

        ASSERT_TRUE(successful);
    }));

    ASSERT_TRUE(loop_published_cb.WaitFor(3000));
    ASSERT_TRUE(test_published_cb.WaitFor(3000));
    ASSERT_TRUE(local_subscription_called.WaitFor(0));
    ASSERT_TRUE(message_published.WaitFor(0));

    // subscribe
    AutoResetEvent signal_1, signal_2;
    ASSERT_S(broker->Subscribe("test_subscription_1", [&](IPayload* data)
    {
        signal_1.Set();
    }));
    int32_t token1 = hr;

    ASSERT_S(broker->Subscribe("test_subscription_2", [&](IPayload* data)
    {
        signal_2.Set();
    }));
    int32_t token2 = hr;

    factory->InvokeSubscription("hello");
    ASSERT_TRUE(signal_1.WaitFor(0));
    ASSERT_FALSE(signal_2.WaitFor(0));

    factory->InvokeSubscription("hello");
    ASSERT_TRUE(signal_1.WaitFor(0));
    ASSERT_FALSE(signal_2.WaitFor(0));

    factory->InvokeSubscription("world");
    ASSERT_FALSE(signal_1.WaitFor(0));
    ASSERT_TRUE(signal_2.WaitFor(0));

    // unsubscribe
    broker->Unsubscribe(token1);
    factory->InvokeSubscription("hello");
    ASSERT_FALSE(signal_1.WaitFor(0));
    ASSERT_FALSE(signal_2.WaitFor(0));

    broker->Unsubscribe(token2);
    factory->InvokeSubscription("world");
    ASSERT_FALSE(signal_1.WaitFor(0));
    ASSERT_FALSE(signal_2.WaitFor(0));
}

void TestRegex()
{
    HRESULT hr = S_OK;

    std::vector<std::string> handled_messages = {
        "test_message_${var1}_${var2}",
        "${var1}test-message${var2}foo"
        };

    MessageIdMatchResults results;
    ASSERT_S(GetMessageIdVariables(&results, "test_message_hello_world", handled_messages));
    ASSERT_TRUE(results.Matches);
    ASSERT_EQ(results.VariableExpansions["${var1}"].compare("hello"), 0);
    ASSERT_EQ(results.VariableExpansions["${var2}"].compare("world"), 0);
    ASSERT_EQ(results.MatchedMessageId, "test_message_${var1}_${var2}");
    ASSERT_EQ(results.VariableExpansions.size(), 2);

    ASSERT_S(GetMessageIdVariables(&results, "test_message_hello_world_foo", handled_messages));
    ASSERT_TRUE(results.Matches);
    ASSERT_EQ(results.VariableExpansions["${var1}"].compare("hello_world"), 0);
    ASSERT_EQ(results.VariableExpansions["${var2}"].compare("foo"), 0);
    ASSERT_EQ(results.MatchedMessageId, "test_message_${var1}_${var2}");
    ASSERT_EQ(results.VariableExpansions.size(), 2);

    ASSERT_S(GetMessageIdVariables(&results, "test_message2_hello_world", handled_messages));
    ASSERT_FALSE(results.Matches);
    ASSERT_EQ(results.MatchedMessageId.length(), 0);
    ASSERT_EQ(results.VariableExpansions.size(), 0);

    ASSERT_S(GetMessageIdVariables(&results, "hellotest-messageworldfoo", handled_messages));
    ASSERT_TRUE(results.Matches);
    ASSERT_EQ(results.VariableExpansions["${var1}"].compare("hello"), 0);
    ASSERT_EQ(results.VariableExpansions["${var2}"].compare("world"), 0);
    ASSERT_EQ(results.MatchedMessageId, "${var1}test-message${var2}foo");
    ASSERT_EQ(results.VariableExpansions.size(), 2);

    std::string config = 
        "{                                                                  "
        "    \"targets\": [                                                 "
        "        {                                                          "
        "            \"protocol\": \"test_protocol\",                       "
        "            \"name\": \"custom_1\",                                "
        "            \"test_protocol_options\": {                           "
        "            }                                                      "
        "        }                                                          "
        "    ],                                                             "
        "    \"pipes\": [                                                   "
        "        {                                                          "
        "            \"message_id\": \"test_message_${var1}_${var2}\",      "
        "            \"destinations\": [                                    "
        "                {                                                  "
        "                    \"target_name\": \"custom_1\",                 "
        "                    \"test_protocol_message_options\": {           "
        "                        \"parameter\": \"${var1}_${var2}\"         "
        "                    }                                              "
        "                }                                                  "
        "            ]                                                      "
        "        }                                                          "
        "    ]                                                              "
        "}                                                                  ";

    ComPtr<IMessageBroker> broker;
    ASSERT_S(MessageBroker::Create(broker.AddressOf(), nullptr, config.c_str()));

    // Add the factory
    AutoResetEvent message_published;
    ComPtr<TestProtocolFactory> factory;
    ASSERT_S(TestProtocolFactory::Create(factory.AddressOf(), [&](ITestMessage* payload)
    {
        ASSERT_EQ(strcmp(payload->Parameter(), "hello_world"), 0);
        message_published.Set();
    }));

    ASSERT_S(broker->AddProtocolFactory(factory));
    ASSERT_S(broker->Initialize());

    broker->Publish("test_message_hello_world", "contents");
    ASSERT_TRUE(message_published.WaitFor(0));

    broker->Publish("test_message_hello", "contents");
    ASSERT_FALSE(message_published.WaitFor(0));
}

TEST(Core, MessageBrokerTests)
{   
    TestExpandMacros();
    TestCreateMessageBroker();
    TestCreateBufferPayload();
    TestCreateBatchPayload();
    TestCreateVideoPayload();
    TestId();
    TestCorrelationId();
    TestTimestamp();
    LocalPublishSubscribe();
    TestBufferPayload();
    TestBatchPayload();
    TestVideoPayload();
    TestCustomProtocol();
    TestRegex();
}
