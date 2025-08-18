#ifndef __VIDEO_CLIP_MANAGER_H__
#define __VIDEO_CLIP_MANAGER_H__

#include <list>

#include <Panorama/videocapture.h>
#include <Panorama/flowcontrol.h>
#include "video_capture_internal.h"

namespace Panorama
{
    /// @brief Purpose of this class to hold onto clips have that been previously created and determine if they can be reused in subsequent GenerateClip calls.
    class VideoClipManager
    {
    public:
        HRESULT GetReusableClips(
            IVideoClipCollection** ppObj, 
            std::list<ComPtr<IVideoPacket>>::const_iterator* unhandled_packets_head,
            const std::list<ComPtr<IVideoPacket>>::const_iterator& begin,
            const std::list<ComPtr<IVideoPacket>>::const_iterator& end, 
            int32_t clip_size)
        {
            HRESULT hr = S_OK;
            CHECKNULL(ppObj, E_POINTER);
            CHECKNULL(unhandled_packets_head, E_POINTER);
            CHECKIF((*begin)->KeyFrame() == false, E_INVALID_STATE);

            // Instantiate the video clip collection
            ComPtr<IVideoClipCollection> collection;
            CHECKHR(VideoCapture::VideoClipCollection(collection.AddressOf()));

            // No clips of this size have been created before, return empty list
            if(_clips.find(clip_size) == _clips.end())
            {
                *ppObj = collection.Detach();
                *unhandled_packets_head = begin;
                return hr;
            }

            // Find the reusable clips
            std::list<ComPtr<IVideoClip>>& candidates = _clips[clip_size];
            std::list<ComPtr<IVideoPacket>>::const_iterator iter = begin;

            // Get the candidate whose start equals the head packets PTS
            // If no candidate matches this then no clip is reusable
            auto candidate = candidates.cbegin();
            for(; candidate != candidates.end(); candidate++)
            {
                if( (*iter)->PTS() == (*candidate)->StartPTS())
                {
                    break;
                }
            }

            // Remove the clips who start before the head packet
            if(candidate != candidates.cbegin())
            {
                candidates.erase(candidates.cbegin(), candidate);
            }

            // If all candidates have been erased then return empty collection
            if(candidates.size() == 0)
            {
                *ppObj = collection.Detach();
                *unhandled_packets_head = begin;
                return hr;
            }

            candidate = candidates.cbegin();

            // Candidate iterator has been moved ahead to the first possible clip that might 
            // satisfy the clip creation.  Finished if all candidates have StartPTS less than Packet Head PTS
            // Find the first Key Frame that isn't associated with a clip
            for(; iter != end && candidate != candidates.end(); iter++, candidate++)
            {
                if(iter != begin)
                {
                    // With the exception of the head packet.  The packet and the current clip MUST share a [Start]PTS.  
                    // If this isn't true then the data structure is flawed
                    __assert((*iter)->PTS() == (*candidate)->StartPTS(), "Previously generated clips and frames are out of alignment");
                }

                // Not possible for this frame to not be a Key frame.  If this isn't true then video clip creation is bugged
                __assert((*iter)->KeyFrame(), "Not starting with an Key frame");

                // Need to skip the packet iterator through to the frame that shares a PTS with the clip::EndPTS
                for(; iter != end; iter++)
                {
                    int64_t pkt_pts = (*iter)->PTS();
                    if((*iter)->PTS() == (*candidate)->EndPTS())
                    {
                        // At the packet that is the last packet in the video clip
                        break;
                    }
                }

                CHECKHR(collection->Add((*candidate)));

                // If packet iterator has reached the end then data structured is flawed
                // Couldn't be managing clips created from not yet received packets
                CHECKIF(iter == end, E_FAIL);
            }

            // packets perfectly aligned with clip
            if(iter == end)
            {
                *ppObj = collection.Detach();
                *unhandled_packets_head = iter;
                return hr;
            }


            // If there are more packets need to decide if the last clip should be recreated or not.
            // Must be recreated if the current packet is not a key frame as we cannot start the next clip without a key frame
            // Possible options to consider could be length of the clip, for now opting to stick with a short video clip to minimize overhead
            if(iter != end && (*iter)->KeyFrame() == false)
            {
                auto last_clip = std::prev(candidates.end());
                collection->PopBack();

                // Reverse iterate through list until we get to the packet that starts the last video clip
                for(; (*iter)->PTS() != (*last_clip)->StartPTS() && iter != begin; iter = std::prev(iter))
                {
                }

                // Neither of this conditions should ever be true.
                // Something has gone terribly wrong if they are so return E_FAIL
                CHECKIF((*iter)->KeyFrame() == false, E_FAIL); // Did not end on a key frame
                CHECKIF((*iter)->PTS() != (*last_clip)->StartPTS(), E_FAIL); // Data not aligned
            }

            *unhandled_packets_head = iter;
            *ppObj = collection.Detach();
            return hr;
        }

        HRESULT AddVideoClip(IVideoClip* clip, int32_t clip_size_ms)
        {
            CHECKNULL(clip, E_INVALIDARG);

            if(_clips.find(clip_size_ms) == _clips.end())
            {
                _clips[clip_size_ms].push_back(clip);
                return S_OK;
            }

            // Due to funkiness of some encoders it's possible for PTS to be before the EndPTS of the previously added clip.
            // Instead check to the Start.
            int64_t end_of_managed_clips = (*std::prev(_clips[clip_size_ms].end()))->StartPTS();
            int64_t start_of_clip_to_add = clip->StartPTS();
            CHECKIF_MSG(end_of_managed_clips >= start_of_clip_to_add, E_OUTOFRANGE, "Adding a clip that starts before the end of the currently managed clips");
            _clips[clip_size_ms].push_back(clip);
            return S_OK;
        }

        void ClearClips(int32_t ms)
        {
            if(_clips.find(ms) != _clips.end())
            {
                _clips[ms].clear();
            }
        }

    private:
        std::map<int32_t, std::list<ComPtr<IVideoClip>>> _clips; 
    };
}

#endif