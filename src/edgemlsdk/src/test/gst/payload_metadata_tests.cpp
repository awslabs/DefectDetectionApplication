#include <gtest/gtest.h>
#include <nlohmann/json.hpp>

#include <Panorama/comptr.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/gst.h>
#include <Panorama/trace.h>
#include <TestUtils.h>

using namespace Panorama;

class CustomPayload : public UnknownImpl<IPayload>
{
public:
    static HRESULT Create(IPayload** ppObj, int x, int y, const char* z)
    {
        COM_FACTORY(CustomPayload, Initialize(x, y, z));
    }

    ~CustomPayload()
    {
        COM_DTOR_FIN(CustomPayload);
    }

    HRESULT Serialize(IBuffer** ppObj) override
    {
        HRESULT hr = S_OK;
        CHECKNULL(ppObj, E_POINTER);
        *ppObj = nullptr;

        size_t sz = 2 * sizeof(int) + _z.size();
        
        ComPtr<IBuffer> ptr;
        CHECKHR(Buffer::Create(ptr.AddressOf(), sz));

        memcpy(ptr->Data(), &_x, sizeof(int32_t));
        memcpy(ptr->Data() + sizeof(int32_t), &_y, sizeof(int32_t));
        memcpy(ptr->Data() + 2*sizeof(int32_t), (void*)(_z.c_str()), _z.size());

        *ppObj = ptr.Detach();
        return hr;
    }

    const char* SerializeAsString() override
    {
        return nullptr;
    }

    const char* Id() override
    {
        return nullptr;
    }

    int64_t Timestamp() override
    {
        return 0;
    }

    HRESULT SetTimestamp(int64_t timestamp) override
    {
        return S_OK;
    }

    const char* CorrelationId() override
    {
        return nullptr;
    }

    HRESULT SetCorrelationId(const char* correlationId) override
    {
        return S_OK;
    }

private:
    HRESULT Initialize(int x, int y, const char* z)
    {
        _x = x;
        _y = y;
        _z = z;
        return S_OK;
    }

    int32_t _x = 0;
    int32_t _y = 0;
    std::string _z;
};

void AddToBuffer()
{
    HRESULT hr = S_OK;
    GstBuffer *buffer = gst_buffer_new_allocate(NULL, 1024, NULL);
    ASSERT_TRUE(buffer != nullptr);

    ComPtr<IPayload> payload1, payload2;
    ComPtr<IBuffer> buffer1, buffer2;
    ASSERT_S(CustomPayload::Create(payload1.AddressOf(), 1, 2, "three"));
    ASSERT_S(CustomPayload::Create(payload2.AddressOf(), 4, 5, "six"));
    ASSERT_S(payload1->Serialize(buffer1.AddressOf()));
    ASSERT_S(payload2->Serialize(buffer2.AddressOf()));

    ASSERT_S(GStreamer::AddPayloadToBuffer(payload1, buffer, "payload1"));
    ASSERT_S(GStreamer::AddPayloadToBuffer(payload2, buffer, "payload2"));

    ComPtr<IPayload> rpayload1, rpayload2;
    ComPtr<IBuffer> rbuffer1, rbuffer2;
    ASSERT_S(GStreamer::GetPayloadFromBuffer(rpayload1.AddressOf(), buffer, "payload1"));
    ASSERT_S(GStreamer::GetPayloadFromBuffer(rpayload2.AddressOf(), buffer, "payload2"));
    ASSERT_S(rpayload1->Serialize(rbuffer1.AddressOf()));
    ASSERT_S(rpayload2->Serialize(rbuffer2.AddressOf()));

    ASSERT_EQ(buffer1->Size(), rbuffer1->Size());
    ASSERT_TRUE(memcmp(buffer1->Data(), rbuffer1->Data(), buffer1->Size()) == 0);

    ASSERT_EQ(buffer2->Size(), rbuffer2->Size());
    ASSERT_TRUE(memcmp(buffer2->Data(), rbuffer2->Data(), buffer1->Size()) == 0);

    gst_buffer_unref(buffer);
}

TEST(GStreamer, PayloadMetaTests)
{
    HRESULT hr = S_OK;
    ASSERT_S(GStreamer::Initialize());

    AddToBuffer();

    GStreamer::Shutdown();
}