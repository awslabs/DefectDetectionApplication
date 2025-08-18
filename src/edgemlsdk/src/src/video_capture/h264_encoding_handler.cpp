
#include <list>
#include <Panorama/unknown.h>
#include <evictable_list.h>
#include <scheduling.h>
#include "video_capture_internal.h"

using namespace Panorama;

struct ClipGenerationJob
{
    std::list<ComPtr<IVideoPacket>> Packets;
    ComPtr<IVideoCaptureEventHandler> Callback;
    int32_t ClipSize;
};

class H264EncodingHandler : public UnknownImpl<IEncodingHandler>
{
public:
    static HRESULT Create(IEncodingHandler** ppObj, int64_t max_duration, uint32_t width, uint32_t height, ContainerFormat container_format)
    {
        COM_FACTORY(H264EncodingHandler, Initialize(max_duration, width, height, container_format));
    }

    ~H264EncodingHandler()
    {
        COM_DTOR(H264EncodingHandler);

        if(_packets != nullptr)
        {
            delete _packets;
        }

        _clip_generation_jobs.Stop();

        COM_DTOR_FIN(H264EncodingHandler);
    }

    // The below public methods could be moved to a base class
    // if/when we ever need to create a different encoding handler
    HRESULT AddPacket(ComPtr<IVideoPacket> packet) override
    {
        HRESULT hr = S_OK;
        CHECKNULL(packet, E_INVALIDARG);
        CHECKHR(_packets->Insert(packet));

        _tail_pts = packet->PTS();
        return hr;
    }

    HRESULT GenerateMultipleClips(int32_t clip_size_ms, IVideoCaptureEventHandler* clip_ready) override
    {
        std::lock_guard<std::mutex> lk(_generate_clips_mtx);
        HRESULT hr = S_OK;
        CHECKNULL(clip_ready, E_INVALIDARG);

        // Get a snapshot of the list.
        ClipGenerationJob job;
        job.Packets = _packets->Snapshot();
        job.ClipSize = clip_size_ms;
        job.Callback = clip_ready;
        _clip_generation_jobs.Enqueue(job, [&](HRESULT result)
        {
            TraceVerbose("Finished video clip generation request: %s", ErrorCodeToString(result));
        });

        return hr;
    }

    int64_t HeadPTS() override
    {
        return _head_pts;
    }

    int64_t TailPTS() override
    {
        return _tail_pts;
    }

private:
    HRESULT Initialize(int64_t max_duration, uint32_t width, uint32_t height, ContainerFormat container_format)
    {
        HRESULT hr = S_OK;

        _width = width;
        _height = height;
        _max_capture_duration = max_duration;
        _container_format = container_format;
        _head_pts = 0;
        _tail_pts = 0;

        _given_exceeding_storage_warning = false;

        _packets = new (std::nothrow) EvictableList<ComPtr<IVideoPacket>, int64_t>(
                    [&](std::list<ComPtr<IVideoPacket>>::const_iterator begin, std::list<ComPtr<IVideoPacket>>::const_iterator end, int32_t count, int64_t duration)
                    {
                        return this->EvictionStrategy(begin, end, count, duration);
                    },
                    [&](const ComPtr<IVideoPacket>& packet)
                    {
                        return packet->Duration();
                    }, 0);

        CHECKNULL(_packets, E_OUTOFMEMORY);

        _clip_generation_jobs.SetProcessor([&](ClipGenerationJob job)
        {
            HRESULT hr = S_OK;

            ComPtr<IVideoClipCollection> clips;
            hr = GenerateMultipleClipsInternal(clips.AddressOf(), job.Packets, job.ClipSize);
            PEEKHR(hr);
            job.Callback->OnVideoClipsGenerated(clips, SUCCEEDED(hr));

            return hr;
        });
        _clip_generation_jobs.Start();

        return hr;
    }

    std::list<ComPtr<IVideoPacket>>::const_iterator EvictionStrategy(std::list<ComPtr<IVideoPacket>>::const_iterator begin, std::list<ComPtr<IVideoPacket>>::const_iterator end, int32_t count, int64_t duration)
    {
        if(duration > _max_capture_duration)
        {
            auto iter = begin;

            // Throw away frames until the next IDR frame that results in a duration that is <= maximum capture duration 
            do
            {
                duration -= (*iter)->Duration();
                iter++;
            } while ((duration <= _max_capture_duration && (*iter)->KeyFrame())==false && iter != end);

            if(iter == end)
            {
                // Distance between key frames is larger than the provided maximum clip.  
                // Clearing frames would, if next frame is not a Key Frame, result in a the head of our list being a non Key frame which we don't support.
                if(_given_exceeding_storage_warning == false)
                {
                    // Don't spam warning
                    TraceWarning("Distance between key frames is larger than your maximum clip duration, storing more video than requested to account for this.  Recommend increase maximum video size or decreasing key frame distance");
                    _given_exceeding_storage_warning = true;
                }

                _head_pts = (*begin)->PTS();
                return begin;
            }

            _head_pts = (*iter)->PTS();
            return iter;
        }

        _head_pts = (*begin)->PTS();
        return begin;
    }

    HRESULT GenerateMultipleClipsInternal(
        IVideoClipCollection** ppObj, 
        std::list<ComPtr<IVideoPacket>>& packets,
        int32_t clip_size_ms)
    {
        HRESULT hr = S_OK;
        CHECKNULL(ppObj, E_POINTER);

        // todo: Make shallow copy of list so we can do this in the background
        int64_t target_duration = static_cast<int64_t>(clip_size_ms) * 1000000; // convert to nano seconds

        // Get the reusable clips
        ComPtr<IVideoClipCollection> clips;
        CHECKHR(VideoCapture::VideoClipCollection(clips.AddressOf()));

        // No packets, return empty list
        if(packets.size() == 0)
        {
            *ppObj = clips.Detach();
            return S_OK;
        }

        // Iterate through the remaing packets and add them to a VideoClip object
        // Once that clip has exceeded it's duration finalize the video clip at the earliest possible opporunity (next IDR frame)
        auto iter = packets.cbegin();
        do
        {
            int64_t clip_duration = 0;

            ComPtr<IVideoClip> clip;
            CHECKHR(VideoCapture::FFMpegVideoClip(clip.AddressOf(), _width, _height, Encoding::H264, _container_format));

            for(; iter != packets.end(); iter++)
            {
                clip_duration += (*iter)->Duration();
                if(clip_duration >= target_duration && (*iter)->KeyFrame()) 
                {
                    // reached the target size and at a key frame
                    // Could do better as it might be closer to the target duration to end early (can work on this later)
                    break;
                }
                else
                {
                    CHECKHR(clip->WriteFrame((*iter)->Data(), (*iter)->PTS(), (*iter)->DTS()));
                }
            }

            // Final clip will be remaining frames
            CHECKHR(clip->Finalize());
            CHECKHR(clips->Add(clip));
        } while (iter != packets.end());

        *ppObj = clips.Detach();
        return hr;
    }

    ContainerFormat _container_format;
    uint32_t _width, _height;
    int64_t _max_capture_duration;
    int64_t _head_pts, _tail_pts;

    EvictableList<ComPtr<IVideoPacket>, int64_t>* _packets;
    bool _given_exceeding_storage_warning;

    JobQueue<ClipGenerationJob> _clip_generation_jobs;
    std::mutex _generate_clips_mtx;
};

extern "C"
{
    HRESULT CreateH264EncodingHandler(IEncodingHandler** ppObj, int64_t max_duration, uint32_t width, uint32_t height, ContainerFormat container_format)
    {
        return H264EncodingHandler::Create(ppObj, max_duration, width, height, container_format);
    }
}