#ifndef __VIDEOCAPTUREINTERNAL_H__
#define __VIDEOCAPTUREINTERNAL_H__

#include <Panorama/apidefs.h>
#include <Panorama/buffer.h>
#include <Panorama/apidefs.h>
#include <Panorama/comptr.h>
#include <Panorama/videocapture.h>

namespace Panorama
{
    DEF_INTERFACE(IVideoPacket, "{38FF5AC1-B3FC-4621-AD46-E4D62874FCE3}", IUnknownAlias)
    {
        virtual bool KeyFrame() = 0;
        virtual ComPtr<IBuffer> Data() = 0;
        virtual int64_t PTS() = 0;
        virtual int64_t DTS() = 0;
        virtual int64_t Duration() = 0;
    };

    DEF_INTERFACE(IEncodingHandler, "{B27CF398-E9EA-4D94-9DAA-E062521B1D5F}", IUnknownAlias)
    {
        virtual HRESULT AddPacket(ComPtr<IVideoPacket> packet) = 0;
        virtual HRESULT GenerateMultipleClips(int32_t clip_size_ms, IVideoCaptureEventHandler* clip_ready) = 0;
        virtual int64_t HeadPTS() = 0;
        virtual int64_t TailPTS() = 0;
    };

    extern "C"
    {
        HRESULT CreateH264EncodingHandler(IEncodingHandler** ppObj, int64_t max_duration, uint32_t width, uint32_t height, ContainerFormat container_format);
        HRESULT CreateH264Packet(IVideoPacket** ppObj, IBuffer* data, int64_t pts, int64_t dts, int64_t duration);
    }
}

#endif