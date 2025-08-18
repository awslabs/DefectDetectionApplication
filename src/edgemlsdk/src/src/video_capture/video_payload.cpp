#include <core/message_broker/payload_base.h>
#include <Panorama/message_broker.h>
#include <Panorama/videocapture.h>

using namespace Panorama;

class VideoPayload : public UnknownImpl<IVideoPayload>, public PayloadBase
{
public:
    static HRESULT Create(IVideoPayload** ppObj, IVideoClip* contents)
    {
        COM_FACTORY(VideoPayload, Initialize(contents));
    }

    ~VideoPayload()
    {
        COM_DTOR_FIN(VideoPayload);
    }

    HRESULT Serialize(IBuffer** ppObj) override
    {
        CHECKNULL(ppObj, E_POINTER);
        _videoClip.AddRef();
        *ppObj = _videoClip.Ptr();
        return S_OK;
    }

    const char* SerializeAsString() override
    {
        return _videoClip->AsString();
    }

    int64_t Duration() override
    {
        return _videoClip->Duration();
    }

    HRESULT SerializeVideoClip(IVideoClip** ppObj) override
    {
        CHECKNULL(ppObj, E_POINTER);
        _videoClip.AddRef();
        *ppObj = _videoClip.Ptr();
        return S_OK;
    }

private:
    VideoPayload() = default;

    HRESULT Initialize(IVideoClip* contents)
    {
        CHECKNULL(contents, E_INVALIDARG);
        _timestamp = NowAsTimestamp();
        _id = GuidToString(GenerateGuid());
        _videoClip = contents;
        return S_OK;
    }

    ComPtr<IVideoClip> _videoClip;
};

DLLAPI HRESULT CreateVideoPayloadFromVideoClip(IVideoPayload** ppObj, IVideoClip* contents)
{
    return VideoPayload::Create(ppObj, contents);
}
