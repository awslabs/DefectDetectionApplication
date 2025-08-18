#include <mutex>
#include <vector>

#include <Panorama/comptr.h>
#include <Panorama/unknown.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/videocapture.h>

using namespace Panorama;

class VideoClipCollection : public UnknownImpl<IVideoClipCollection>
{
public:
    static HRESULT Create(IVideoClipCollection** ppObj)
    {
        COM_FACTORY(VideoClipCollection, Initialize());
    }

    ~VideoClipCollection()
    {
        COM_DTOR_FIN(VideoClipCollection);
    }

    int32_t Count() override
    {
        std::lock_guard<std::mutex> lk(_mtx);
        return _clips.size();
    }

    HRESULT Clip(IVideoClip** ppObj, int32_t idx) override
    {
        std::lock_guard<std::mutex> lk(_mtx);
        CHECKIF(idx < 0 || idx >= _clips.size(), E_OUTOFRANGE);
        _clips[idx].AddRef();
        *ppObj = _clips[idx];
        return S_OK;
    }

    HRESULT Add(IVideoClip* clip) override
    {
        CHECKNULL(clip, E_INVALIDARG);
        std::lock_guard<std::mutex> lk(_mtx);
        _clips.push_back(clip);
        return S_OK;
    }

    void PopBack() override
    {
        _clips.pop_back();
    }

private:
    VideoClipCollection() = default;

    HRESULT Initialize()
    {
        return S_OK;
    }

    std::mutex _mtx;
    std::vector<ComPtr<IVideoClip>> _clips;
};

DLLAPI HRESULT CreateVideoClipCollection(IVideoClipCollection** ppObj)
{
    return VideoClipCollection::Create(ppObj);
}