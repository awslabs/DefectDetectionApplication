#include <queue>

extern "C" {
    #include <libavcodec/avcodec.h>
    #include <libavformat/avformat.h>
    #include <libavutil/avutil.h>
    #include <libavutil/time.h>
    #include <libavutil/opt.h>
    #include <libswscale/swscale.h>
    #include <libavutil/mathematics.h>
    #include <libavutil/timestamp.h>
    #include <libswresample/swresample.h>
    #include <libswresample/swresample.h>
}

#include <Panorama/flowcontrol.h>
#include <Panorama/comptr.h>
#include <Panorama/comobj.h>
#include <Panorama/videocapture.h>

using namespace Panorama;

#define NANOSEC_TO_SECOND 0.000000001
#define PACKET_SIZE 4096 // 4 KB (size of the buffer to hold the data to be written to the video stream)
#define CHUNK_SIZE 1048576 // 1 MB (size of a chunk of the video clip being created)

std::string output_file;
struct SeekableMem
{
    uint8_t* Bytes = nullptr;
    int64_t Length;
    int64_t AllocatedMemLength;
    int64_t CurrPosition;
};

static int64_t seek(void *opaque, int64_t offset, int whence)
{
    SeekableMem* data = reinterpret_cast<SeekableMem*>(opaque);

    switch(whence)
    {
        case SEEK_SET:
            data->CurrPosition = offset;
            break;
        case SEEK_CUR:
            data->CurrPosition += offset;
            break;
        case SEEK_END:
            data->CurrPosition = data->Length - offset;
            break;
        default:
            TraceError("Whence = %d", whence);
            return -1;
    }

    return data->CurrPosition;
}

static int write_memory(void *opaque, uint8_t *buf, int buf_size) 
{
    SeekableMem* data = reinterpret_cast<SeekableMem*>(opaque);
    
    // Validate we have allocated enough memory to write the new data
    int64_t next_pos = data->CurrPosition + buf_size;
    if(next_pos > data->AllocatedMemLength)
    {
        // writing memory would go beyond our currently allocated memory.  Allocate more
        int32_t multiplier = 1 + (next_pos - data->AllocatedMemLength) / CHUNK_SIZE;
        data->AllocatedMemLength = data->AllocatedMemLength + multiplier * CHUNK_SIZE;
        data->Bytes = reinterpret_cast<uint8_t*>(realloc(data->Bytes, data->AllocatedMemLength));
        if(data->Bytes == nullptr)
        {
            TraceError("Could not allocate memory for video clip, likely out of memory");
            return -1;
        }
    }

    // Write the data from the current position
    uint8_t* ptr = data->Bytes + data->CurrPosition;
    memcpy(ptr, buf, buf_size);
    data->CurrPosition = next_pos;

    // Update the length of the content
    if(data->CurrPosition > data->Length)
    {
        data->Length = data->CurrPosition;
    }

    return buf_size;
}


class FFMpegVideoClip : public UnknownImpl<IVideoClip>
{
public:
    static HRESULT Create(IVideoClip** ppObj, uint32_t width, uint32_t height, Encoding encoding, ContainerFormat container_format)
    {
        COM_FACTORY(FFMpegVideoClip, Initialize(width, height, encoding, container_format));
    }

    ~FFMpegVideoClip()
    {
        COM_DTOR(FFMpegVideoClip);

        if(_avio_ctx_buffer != nullptr)
        {
            av_free(_avio_ctx_buffer);
        }

        if(_avio_ctx != nullptr)
        {
            av_freep(_avio_ctx);
        }

        if(_data.Bytes != nullptr)
        {
            free(_data.Bytes);
        }

        COM_DTOR_FIN(FFMpegVideoClip);
    }

    int64_t StartPTS() override
    {
        return _start_pts;
    }

    int64_t EndPTS() override
    {
        return _end_pts;
    }

    int64_t Duration() override
    {
        return _duration;
    }

    uint8_t* Data() const override
    {
        return _finalized_data;
    }

    int32_t Size() const override
    {
        return _finalized_size;
    }

    const char* AsString() const override
    {
        return _to_string.c_str();
    }

    HRESULT WriteFrame(IBuffer* data, int64_t pts, int64_t dts) override
    {
        HRESULT hr = S_OK;
        CHECKNULL(data, E_INVALIDARG);

        AVPacket* pkt = av_packet_alloc();

        if(_start_pts == -1)
        {
            _start_pts = pts;
        }

        _end_pts = pts;
        _duration = _end_pts - _start_pts;

        pkt->data = data->Data();
        pkt->size = data->Size();

        if(_epoch == -1)
        {
            _epoch = pts < dts ? pts : dts;
        }

        double time_base = av_q2d(_stream->time_base); // seconds per time unit of the video container
        pkt->pts = static_cast<int64_t>((static_cast<double>(pts - _epoch) * NANOSEC_TO_SECOND) / time_base);
        pkt->dts = static_cast<int64_t>((static_cast<double>(dts - _epoch) * NANOSEC_TO_SECOND) / time_base);

        CHECK_FAIL(av_interleaved_write_frame(_ofctx, pkt), E_FAIL);

        av_packet_unref(pkt); // Free the packet
        return S_OK;
    }

    HRESULT Finalize() override
    {
        HRESULT hr = S_OK;

        if (_ofctx)
        {
            CHECK_FAIL(av_write_trailer(_ofctx), E_FAIL);
            avformat_free_context(_ofctx);
            _ofctx = NULL;
        }

        _finalized_data = _data.Bytes;
        _finalized_size = _data.Length;

        return S_OK;
    }

private:
    HRESULT Initialize(uint32_t width, uint32_t height, Encoding encoding, ContainerFormat container_format)
    {
        HRESULT hr = S_OK;

// Defined through CMakeLists (src/video_capture/CMakeLists.txt)
#ifdef OLDER_FFMPEG
        static std::mutex ffmpeg_mtx;
        static bool ffmpeg_initialized = false;
        {
            std::lock_guard<std::mutex> lk(ffmpeg_mtx);
            if(ffmpeg_initialized == false)
            {
                av_register_all();
                ffmpeg_initialized = true;
            }
        }
#endif

        _to_string = "FFMpegVideoClip does not implement AsString()";
        _start_pts = -1;
        _duration = 0;
        
        // Allocate a chunk of memory to hold the resulting video
        _data.Bytes = reinterpret_cast<uint8_t*>(malloc(CHUNK_SIZE));
        CHECKNULL(_data.Bytes, E_OUTOFMEMORY);
        _data.Length = 0;
        _data.CurrPosition = 0;
        _data.AllocatedMemLength = CHUNK_SIZE;

        // Allocate FFmpeg internal buffer
        // Will contain the buffer currently in use
        unsigned char* avio_ctx_buffer = static_cast<unsigned char*>(av_malloc(PACKET_SIZE));
        CHECKNULL(avio_ctx_buffer, E_FAIL);

        // Create AVIOContext
        AVIOContext* _avio_ctx = avio_alloc_context(avio_ctx_buffer, PACKET_SIZE, 1, &_data, nullptr, write_memory, seek);
        CHECKNULL(_avio_ctx, E_FAIL);

        std::string c_format;
        switch(container_format)
        {
            case ContainerFormat::MP4:
                c_format = "mp4";
                break;
            default:
                TraceError("Container format not supported");
                return E_NOTIMPL;
        }

        int32_t res = avformat_alloc_output_context2(&_ofctx, nullptr, c_format.c_str(), nullptr);
        if(res < 0)
        {
            TraceError("FFmpeg, avformat_alloc_output_context2 returned error code %d", res);
            return E_FAIL;
        }

        _ofctx->pb = _avio_ctx;
        _ofctx->flags |= AVFMT_FLAG_CUSTOM_IO;

        // Add a video stream
        _stream = avformat_new_stream(_ofctx, nullptr);
        _stream->time_base = (AVRational){1, 30}; // Set for your frame rate

        AVCodecParameters* codecParams = _stream->codecpar;

        // Set the codec ic
        switch(encoding)
        {
            case Encoding::H264:
                codecParams->codec_id = AV_CODEC_ID_H264;
                break;
            default:
                TraceError("Encoding not supported");
                return E_NOTIMPL;
        }

        // I don't think this is necessary, leaving it for future reference
        // Set the pixel format
        // switch (format)
        // {
        //     case PixelFormat::NV12:
        //         codecParams->format = AV_PIX_FMT_NV12;
        //         break;
        //     case PixelFormat::NV21:
        //         codecParams->format = AV_PIX_FMT_NV21;
        //         break;
        //     default:
        //         TraceError("Format not supported");
        //         return E_NOTIMPL;
        // }

        codecParams->codec_type = AVMEDIA_TYPE_VIDEO;
        codecParams->width = width;
        codecParams->height = height;

        // Write file header
        CHECK_FAIL(avformat_write_header(_ofctx, nullptr), E_FAIL);

        _finalized_data = nullptr;
        _finalized_size = 0;

        return S_OK;
    }

    int64_t _epoch = -1;
    unsigned char* _avio_ctx_buffer = nullptr;
    SeekableMem _data;
    AVIOContext* _avio_ctx;
    AVOutputFormat* _oformat = nullptr;
    AVFormatContext* _ofctx = nullptr;
    AVCodec* _codec = nullptr;
    AVStream* _stream = nullptr;

    uint8_t* _finalized_data;
    int64_t _finalized_size;

    int64_t _start_pts;
    int64_t _duration;
    int64_t _end_pts;

    std::string _to_string;
};

DLLAPI HRESULT CreateFFmpegVideoClip(IVideoClip** ppObj, uint32_t width, uint32_t height, Encoding encoding, ContainerFormat container_format)
{
    return FFMpegVideoClip::Create(ppObj, width, height, encoding, container_format);
}