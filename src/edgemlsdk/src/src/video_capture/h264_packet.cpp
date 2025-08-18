#include <list>
#include <Panorama/unknown.h>
#include <Panorama/flowcontrol.h>

#include "video_capture_internal.h"

using namespace Panorama;

bool IsIDR(uint8_t* buf, int32_t buf_len) 
{
    // Assumption: NALU in Annex B format
    // NALU will be preceeded by start code which can take the following form
    // 0x00 0x00 0x00 0x01
    // 0x00 0x00 0x01
    // NALU type is contained in byte after start code. 
    // IDR NALU will be value 0??0 0101 (Full Image Data)
    // Helpful article: https://stackoverflow.com/questions/24884827/possible-locations-for-sequence-picture-parameter-sets-for-h-264-stream
    // 31 = 0001 1111 (0??1 0101 is not IDR)
    // todo: Optimize this later if needed
    for (int32_t idx = 0; idx < buf_len - 5; idx++) 
    {
        if (buf[idx] == 0x00 && buf[idx + 1] == 0x00 && buf[idx + 2] == 0x01) 
        {
            if((buf[idx+3] & 31) == 5 /*0101*/)
            {
                return true;
            }
        }

        if (buf[idx] == 0x00 && buf[idx + 1] == 0x00 && buf[idx + 2] == 0x00 && buf[idx + 3] == 0x01) 
        {
            if((buf[idx+4] & 31) == 5 /*0101*/)
            {
                return true;
            }
        }
    }

    return false;
}

class H264Packet : public UnknownImpl<IVideoPacket>
{
public:
    static HRESULT Create(IVideoPacket** ppObj, ComPtr<IBuffer> data, int64_t pts, int64_t dts, int64_t duration)
    {
        COM_FACTORY(H264Packet, Initialize(data, pts, dts, duration));
    }

    ~H264Packet()
    {
        COM_DTOR_FIN(H264Packet);
    }

    ComPtr<IBuffer> Data() override
    {
        return _data;
    }

    bool KeyFrame() override
    {
        return _idr;
    }

    int64_t PTS() override
    {
        return _pts;
    }

    int64_t DTS() override
    {
        return _dts;
    }

    int64_t Duration() override
    {
        return _duration;
    }

private:
    HRESULT Initialize(ComPtr<IBuffer> data, int64_t pts, int64_t dts, int64_t duration)
    {
        CHECKNULL(data, E_INVALIDARG);
        _pts = pts;
        _dts = dts;
        _duration = duration;
        _data = data;
        _idr = IsIDR(data->Data(), data->Size());
        return S_OK;
    }

    int64_t _pts, _dts, _duration;
    bool _idr;
    ComPtr<IBuffer> _data;
};

extern "C"
{
    HRESULT CreateH264Packet(IVideoPacket** ppObj, IBuffer* data, int64_t pts, int64_t dts, int64_t duration)
    {
        return H264Packet::Create(ppObj, data, pts, dts, duration);
    }
}