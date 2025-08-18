#ifndef __VIDEOCAPTURE_H__
#define __VIDEOCAPTURE_H__

#include <limits.h>
#include <functional>

#include <Panorama/apidefs.h>
#include <Panorama/buffer.h>
#include <Panorama/comptr.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/message_broker.h>

namespace Panorama
{
    DEF_INTERFACE(IVideoClip, "{A5268515-A25B-4E2F-B4BF-AFC09B905EA2}", IBuffer)
    {
        /// @brief Gets the first PTS of the clip in nanoseconds
        virtual int64_t StartPTS() = 0;

        /// @brief Gets the last PTS of the clip in nanoseconds
        virtual int64_t EndPTS() = 0;

        /// @brief Gets the duration of the video clip in nanoseconds (equal to EndPTS() - StartPTS())
        virtual int64_t Duration() = 0;

        /// @brief Write data to the video clip
        /// @param data The data to write
        /// @param pts The presentation time
        /// @param dts The decode time
        /// @return S_OK (0) on success.  Error code otherwise
        virtual HRESULT WriteFrame(IBuffer* data, int64_t pts, int64_t dts) = 0;

        /// @brief Finalizes the video clip.  The clip cannot be written to after this is called.
        /// @return S_OK (0) on success.  Error code otherwise
        virtual HRESULT Finalize() = 0;
    };

    DEF_INTERFACE(IVideoClipCollection, "{8FDB83B5-3E25-4CC0-9B51-B7D87120E820}", IUnknownAlias)
    {
        /// @brief Gets the number of clips in the collection
        virtual int32_t Count() = 0;

        /// @brief Gets an IVideoClip at a specified index
        /// @param ppObj Pointer to the retrieved video clip
        /// @param idx Index of the clip to be retrieved
        /// @return S_OK (0) on success.  Error code otherwise.
        virtual HRESULT Clip(IVideoClip** ppObj, int32_t idx) = 0;

        /// @brief Add a video clip to the collection
        /// @param clip The clip to add
        /// @return S_OK (0) on success.  Error code otherwise.
        virtual HRESULT Add(IVideoClip* clip) = 0;

        /// @brief Last element is erased
        virtual void PopBack() = 0;

        inline ComPtr<IVideoClip> GetClip(int32_t idx)
        {
            ComPtr<IVideoClip> clip;
            return SUCCEEDED(this->Clip(clip.AddressOf(), idx)) ? clip : nullptr;
        }
    };

    DEF_INTERFACE(IVideoPayload, "{D9DE3086-5442-4D03-897B-D641E22ED533}", IPayload)
    {
        /// @brief Gets the duration of this video payload
        virtual int64_t Duration() = 0;

            
        /// @brief Serializes the IVideoPayload to an IVideoClip. Mostly for convenience, since Serialize() returns the same buffer, just not specifically as an IVideoClip
        /// @param ppObj IVideoClip to store the results
        /// @return S_OK on success. Error Code on failure
        virtual HRESULT SerializeVideoClip(IVideoClip** ppObj) = 0;
    };

    DEF_INTERFACE(IVideoCaptureEventHandler, "{37841AD2-F2A1-46DD-B881-79B326CD7EFD}", IUnknownAlias)
    {
        /// @brief Called video clips from the video capture request has completed
        /// @param clips The collection of generated video clips
        /// @param successful Flag indicating success or failure
        virtual void OnVideoClipsGenerated(IVideoClipCollection* clips, bool successful) = 0;
    };

    typedef std::function<void(IVideoClipCollection* clips, bool successful)> VideoClipsGenerateCallback;
    class VideoCaptureEventHandler : public UnknownImpl<IVideoCaptureEventHandler>
    {
    public:
        static HRESULT Create(IVideoCaptureEventHandler** ppObj, VideoClipsGenerateCallback cb);

        ~VideoCaptureEventHandler();
        void OnVideoClipsGenerated(IVideoClipCollection* clips, bool successful) override;

    private:
        VideoClipsGenerateCallback _clips_generated_cb;
    };


    DEF_INTERFACE(IVideoCapture, "{9BC9CEEB-8110-4338-BD17-D56D4949CEF1}", IUnknownAlias)
    {
        /// @brief Adds an image/encoded packet to the video stream
        /// @param packet The packet to add
        /// @param pts Presentation Timestamp in nanoseconds
        /// @param dts Decode Timestamp in nanoseconds
        /// @param duration Duration of the frame in nanoseconds
        /// @return S_OK (0) on success.  Error code otherwise.
        virtual HRESULT AddFrame(IBuffer* data, int64_t pts, int64_t dts, int64_t duration) = 0;

        /// @brief Generates a single video clip of all the data currently in the video capture object
        /// @param ppObj Pointer to the collection of video clips
        /// @return S_OK (0) on success.  Error code otherwise.
        /// @note Assumption that the length of video to be captured will be defined in the constructor of a particular implementation
        virtual HRESULT GenerateClip(IVideoClip** ppObj) = 0;

        /// @brief Generates a single video clip of all the data currently in the video capture object
        /// @param ppObj Pointer to the collection of video clips
        /// @param clip_size_ms Length, in milliseconds, the desired clips should be.  Clips will be approximately that length
        /// @return S_OK (0) on success.  Error code otherwise.
        /// @note Assumption that the length of video to be captured will be defined in the constructor of a particular implementation
        virtual HRESULT GenerateMultipleClips(IVideoClipCollection** ppObj, int32_t clip_size_ms) = 0;

        /// @brief Generates video clips from the captured video stream and calls into event handler on generation of the video clips
        /// @param clip_size_ms Length, in milliseconds, the desired clips should be.  Clips will be approximately that length
        /// @param ts Timestamp used as the reference point for controlling how much of past/future video to capture.  If not specified will assume the end of the current video captured as the reference point.
        /// @param pos Value between 0 and 1 to define where reference timestamp resides in captured video (0 = beginning, 0.5 = middle, 1.0=end)
        /// @return S_OK (0) on success.  Error code otherwise.
        /// @note Assumption that the length of video to be captured will be defined in the constructor of a particular implementation
        virtual HRESULT GenerateMultipleClipsAsync(IVideoCaptureEventHandler* handler, int32_t clip_size_ms, int64_t ref_ts=-1, float pos=1.0f) = 0;

        inline HRESULT GenerateMultipleClipsAsync(int32_t clip_size_ms, int64_t ref_ts, float pos, VideoClipsGenerateCallback cb)
        {
            HRESULT hr = S_OK;
            ComPtr<IVideoCaptureEventHandler> handler;
            CHECKHR(VideoCaptureEventHandler::Create(handler.AddressOf(), std::move(cb)));
            CHECKHR(this->GenerateMultipleClipsAsync(handler, clip_size_ms, ref_ts, pos));
            return hr;
        }

        inline HRESULT GenerateMultipleClipsAsync(int64_t ref_ts, float pos, VideoClipsGenerateCallback cb)
        {
            return GenerateMultipleClipsAsync(INT_MAX, ref_ts, pos, cb);
        }
    };

    enum Encoding
    {
        None,
        H264
    };

    enum ContainerFormat
    {
        MP4
    };

    DLLAPI HRESULT CreateVideoCapture(IVideoCapture** ppObj, uint32_t capture_size_ms, uint32_t width, uint32_t height, Encoding encoding, ContainerFormat container_format);
    DLLAPI HRESULT CreateFFmpegVideoClip(IVideoClip** ppObj, uint32_t width, uint32_t height, Encoding encoding, ContainerFormat container_format);
    DLLAPI HRESULT CreateVideoClipCollection(IVideoClipCollection** ppObj);
    DLLAPI HRESULT CreateVideoPayloadFromVideoClip(IVideoPayload** ppObj, IVideoClip* contents);

    class VideoCapture
    {
    public:
        /// @brief Creates an instance of the FFMpeg implementation of the IVideoCapture interface
        /// @param ppObj Pointer to the created IVideoCapture object
        /// @param capture_size_ms Length, in milliseconds, of the amount of video data to hold in memory
        /// @param width Width of the encoded image
        /// @param height Height of the encoded image
        /// @param container_format Format of the video container to create
        /// @return S_OK (0) on success.  Error code otherwise.
        static HRESULT Create(IVideoCapture** ppObj, uint32_t capture_size_ms, uint32_t width, uint32_t height, Encoding encoding, ContainerFormat container_format)
        {
            return CreateVideoCapture(ppObj, capture_size_ms, width, height, encoding, container_format);
        }

        /// @brief Create an implement of IVideoClip that wraps FFmpeg
        /// @param ppObj Pointer to the created IVideoClip object
        /// @param width Width of the video
        /// @param height Height of the video
        /// @param encoding Type of encoding
        /// @param container_format The video format of the output container (e.g. 'mp4')
        /// @return S_OK (0) on success.  Error code otherwise.
        static HRESULT FFMpegVideoClip(IVideoClip** ppObj, uint32_t width, uint32_t height, Encoding encoding, ContainerFormat container_format)
        {
            return CreateFFmpegVideoClip(ppObj, width, height, encoding, container_format);
        }

        /// @brief Creates a video clip collection
        /// @param ppObj Pointer to the created IVideoClip object
        /// @return S_OK (0) on success.  Error code otherwise.
        static HRESULT VideoClipCollection(IVideoClipCollection** ppObj)
        {
            return CreateVideoClipCollection(ppObj);
        }

        /// @brief Creates a video payload from a video clip
        /// @param ppObj Pointer to the created video payload
        /// @param contents The buffer to contain in the video payload
        /// @return S_OK on success, error code otherwise.
        static HRESULT VideoPayload(IVideoPayload** ppObj, IVideoClip* contents)
        {
            return CreateVideoPayloadFromVideoClip(ppObj, contents);
        }
    };
}

#endif