#include <Panorama/message_broker.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/aws.h>

#include <core/message_broker/protocol_client_base.h>

#include <misc.h>

using namespace Panorama;

class S3Message : public ProtocolMessageBase<IS3Message>
{
public:
    static HRESULT Create(IS3Message** ppObj, IPayload* payload, const char* bucket, const char* key, bool overwrite, bool batch_payload_expansion)
    {
        COM_FACTORY(S3Message, Initialize(payload, bucket, key, overwrite, batch_payload_expansion));
    }

    const char* Bucket() override
    {
        return _bucket.c_str();
    }

    const char* Key() override
    {
        return _key.c_str();
    }

    bool Overwrite() override
    {
        return _overwrite;
    }

    bool BatchPayloadExpansion() override
    {
        return _batch_payload_expansion;
    }

    ~S3Message()
    {
        COM_DTOR_FIN(S3Message);
    }

private:
    HRESULT Initialize(IPayload* payload, const char* bucket, const char* key, bool overwrite, bool batch_payload_expansion)
    {
        HRESULT hr = S_OK;

        CHECKHR(InitializeBase(payload));
        CHECKNULL_OR_EMPTY(bucket, E_INVALIDARG);
        CHECKNULL_OR_EMPTY(key, E_INVALIDARG);

        _bucket = bucket;
        _key = key;
        _overwrite = overwrite;
        _batch_payload_expansion = batch_payload_expansion;

        return hr;
    }

    std::string _bucket;
    std::string _key;
    bool _overwrite = false;
    bool _batch_payload_expansion = false;
};

DLLAPI HRESULT CreateS3Message(IS3Message** ppObj, IPayload* payload, const char* bucket, const char* key, bool overwrite, bool batch_payload_expansion)
{
    return S3Message::Create(ppObj, payload, bucket, key, overwrite, batch_payload_expansion);
}