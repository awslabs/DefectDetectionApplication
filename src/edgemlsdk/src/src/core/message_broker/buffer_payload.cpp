#include <core/message_broker/payload_base.h>
#include <Panorama/chrono.h>
#include <Panorama/message_broker.h>

using namespace Panorama;

class BufferPayload : public UnknownImpl<IPayload>, public PayloadBase
{
public:
    static HRESULT Create(IPayload** ppObj, IBuffer* contents)
    {
        COM_FACTORY(BufferPayload, Initialize(contents));
    }

    ~BufferPayload()
    {
        COM_DTOR_FIN(BufferPayload);
    }

    HRESULT Serialize(IBuffer** ppObj) override
    {
        CHECKNULL(ppObj, E_POINTER);
        _buffer.AddRef();
        *ppObj = _buffer.Ptr();
        return S_OK;
    }

    const char* SerializeAsString() override
    {
        return _buffer->AsString();
    }

private:
    BufferPayload() = default;

    HRESULT Initialize(IBuffer* contents)
    {
        CHECKNULL(contents, E_INVALIDARG);
        _timestamp = NowAsTimestamp();
        _id = GuidToString(GenerateGuid());
        _buffer = contents;
        return S_OK;
    }

    ComPtr<IBuffer> _buffer;
};

DLLAPI HRESULT CreatePayloadFromString(IPayload** ppObj, const char* contents)
{
    HRESULT hr = S_OK;
    ComPtr<IBuffer> buffer;
    CHECKHR(CreateBufferFromString(buffer.AddressOf(), contents));
    return BufferPayload::Create(ppObj, buffer);
}

DLLAPI HRESULT CreatePayloadFromBuffer(IPayload** ppObj, IBuffer* contents)
{
    return BufferPayload::Create(ppObj, contents);
}
