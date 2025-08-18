#include <Panorama/flowcontrol.h>
#include <Panorama/comptr.h>
#include <Panorama/comobj.h>
#include <Panorama/videocapture.h>
#include <evictable_list.h>
#include <memory>
#include <scheduling.h>

#include "video_capture_internal.h"

using namespace Panorama;

struct FutureClipGeneration
{
    int64_t ReferenceTime;
    float Pos;
    ComPtr<IVideoCaptureEventHandler> EventHandler;
    int32_t ClipSize;
};

class VideoCaptureImpl : public UnknownImpl<IVideoCapture>
{
public:
    static HRESULT Create(IVideoCapture** ppObj, uint32_t capture_size_ms, uint32_t width, uint32_t height, Encoding encoding, ContainerFormat container_format)
    {
        COM_FACTORY(VideoCaptureImpl, Initialize(capture_size_ms, width, height, encoding, container_format));
    }

    ~VideoCaptureImpl()
    {
        COM_DTOR_FIN(VideoCaptureImpl);
    }

    HRESULT AddFrame(IBuffer* data, int64_t pts, int64_t dts, int64_t duration) override
    {
        HRESULT hr = S_OK;

        ComPtr<IVideoPacket> packet;
        switch(_encoding)
        {
            case Encoding::H264:
                CHECKHR(CreateH264Packet(packet.AddressOf(), data, pts, dts, duration));
                CHECKHR(_encoding_handler->AddPacket(packet));
                break;
            default:
                return E_NOTIMPL;
        }

        // Determine if any future clip generations should start
        StartFutureClipGenerationRequest(_encoding_handler->HeadPTS(), _encoding_handler->TailPTS());
        return hr;
    }

    HRESULT GenerateClip(IVideoClip** ppObj) override
    {
        HRESULT hr = S_OK;
        CHECKNULL(ppObj, E_POINTER);
        ComPtr<IVideoClipCollection> collection;
        CHECKHR(GenerateMultipleClips(collection.AddressOf(), INT32_MAX));
        CHECKHR(collection->Clip(ppObj, 0));
        return hr;
    }

    HRESULT GenerateMultipleClips(IVideoClipCollection** ppObj, int32_t clip_size_ms) override
    {
        HRESULT hr = S_OK;
        CHECKNULL(ppObj, E_POINTER);
        CHECKIF(clip_size_ms <= 0, E_OUTOFRANGE);

        AutoResetEvent clips_ready;
        ComPtr<IVideoClipCollection> results;
        bool success = false;

        ComPtr<IVideoCaptureEventHandler> event_handler;
        CHECKHR(VideoCaptureEventHandler::Create(event_handler.AddressOf(), [&](IVideoClipCollection* collection, bool successful)
        {
            success = successful;
            results = collection;
            clips_ready.Set();
        }));

        CHECKHR(_encoding_handler->GenerateMultipleClips(clip_size_ms, event_handler));
        clips_ready.Wait();
        CHECKIF(success == false, E_FAIL);
        *ppObj = results.Detach();
        return hr;
    }

    HRESULT GenerateMultipleClipsAsync(IVideoCaptureEventHandler* handler, int32_t clip_size_ms, int64_t ref_ts, float pos) override
    {
        CHECKNULL(handler, E_INVALIDARG);
        CHECKIF(pos < 0 || pos > 1.0f, E_OUTOFRANGE);

        FutureClipGeneration future_clip;
        future_clip.ReferenceTime = ref_ts;
        future_clip.Pos = pos;
        future_clip.EventHandler = handler;
        future_clip.ClipSize = clip_size_ms;

        std::lock_guard<std::mutex> lk(_futures_mtx);
        _futures.push_back(future_clip);
        return S_OK;
    }

private:
    VideoCaptureImpl() = default;

    HRESULT Initialize(uint32_t capture_size_ms, uint32_t width, uint32_t height, Encoding encoding, ContainerFormat container_format)
    {
        HRESULT hr = S_OK;
        CHECKIF_MSG(encoding != Encoding::H264, E_NOTIMPL, "Currently only H264 encoding is supported");
        CHECKIF(capture_size_ms == 0, E_OUTOFRANGE);
        CHECKIF(width == 0, E_OUTOFRANGE);
        CHECKIF(height == 0, E_OUTOFRANGE);

        _encoding = encoding;
        _width = width;
        _height = height;
        _max_capture_duration = static_cast<int64_t>(capture_size_ms) * 1000000;
        _container_format = container_format;

        CHECKHR(CreateH264EncodingHandler(_encoding_handler.AddressOf(), _max_capture_duration, _width, _height, _container_format));
        return hr;
    }

    HRESULT StartFutureClipGenerationRequest(int64_t head, int64_t tail)
    {
        // Loop through async generation requests and determine if they need to be started
        HRESULT hr = S_OK;

        double fhead = static_cast<double>(head);
        double ftail = static_cast<double>(tail);
        double divisor = 1.0 / _max_capture_duration;

        std::lock_guard<std::mutex> lk(_futures_mtx);
        auto iter = _futures.begin();
        while(iter != _futures.end())
        {
            double ref_time = static_cast<double>(iter->ReferenceTime);
            double pos = (ref_time - fhead) * divisor;
            if(pos <= iter->Pos || iter->ReferenceTime == -1)
            {
                TraceInfo("Starting video capture for Reference Timestamp %lld @ %f", iter->ReferenceTime, iter->Pos);
                CHECKHR(_encoding_handler->GenerateMultipleClips(iter->ClipSize, iter->EventHandler));
                iter = _futures.erase(iter);
            }
            else
            {
                iter++;
            }
        };

        return hr;
    }

    Encoding _encoding;
    ContainerFormat _container_format;
    uint32_t _width, _height;
    int64_t _max_capture_duration;

    ComPtr<IVideoClip> _clip;
    ComPtr<IEncodingHandler> _encoding_handler;
    std::list<FutureClipGeneration> _futures;
    std::mutex _futures_mtx;
};

DLLAPI HRESULT CreateVideoCapture(IVideoCapture** ppObj, uint32_t capture_size_ms, uint32_t width, uint32_t height, Encoding encoding, ContainerFormat container_format)
{
    return VideoCaptureImpl::Create(ppObj, capture_size_ms, width, height, encoding, container_format);
}