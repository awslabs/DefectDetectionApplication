#include <nlohmann/json.hpp>

#include <Panorama/comptr.h>
#include <Panorama/videocapture.h>
#include <Panorama/gst.h>
#include <Panorama/app.h>
#include <TestUtils.h>

using namespace Panorama;

HRESULT ValidateVideoFile(const std::string& filepath)
{
    HRESULT hr = S_OK;
    std::string command = "ffmpeg -i " + filepath + " -f null -";
    int result = system(command.c_str());
    TraceInfo("Validate Video File Result = %d", result);
    CHECKIF(result != 0, E_FAIL);
    return hr;
}

HRESULT LoadFrame(IBuffer** ppObj, int64_t* pts, int64_t* dts, int64_t* duration, const char* frame_file, const char* meta_file)
{
    HRESULT hr = S_OK;

    // Load buffer
    {
        FILE* fptr = fopen(frame_file, "rb");
        CHECKNULL_MSG(fptr, E_FAIL, "%s", frame_file);

        fseek(fptr, 0, SEEK_END);
        long sz = ftell(fptr);
        fseek(fptr, 0, SEEK_SET);

        ComPtr<IBuffer> buffer;
        CHECKHR(Buffer::Create(buffer.AddressOf(), sz));

        CHECKIF(fread(buffer->Data(), buffer->Size(), sizeof(uint8_t), fptr) == 0, E_FAIL);
        fclose(fptr);

        *ppObj = buffer.Detach();
    }

    // Load meta
    {
        FILE* fptr = fopen(meta_file, "rb");
        CHECKNULL(fptr, E_FAIL);

        fseek(fptr, 0, SEEK_END);
        long sz = ftell(fptr);
        fseek(fptr, 0, SEEK_SET);

        char* meta = (char*)malloc(sz);
        CHECKIF(fread(meta, sz, sizeof(uint8_t), fptr) == 0, E_FAIL);
        fclose(fptr);

        nlohmann::json jObj = nlohmann::json::parse(meta);
        *pts = static_cast<int64_t>(jObj["pts"]);
        *dts = static_cast<int64_t>(jObj["dts"]);
        *duration = static_cast<int64_t>(jObj["duration"]);
    }

    return hr;
}

void FFmpegVideoClip_H264_MP4(const std::string& dir)
{
    HRESULT hr = S_OK;

    ComPtr<IVideoClip> clip;
    ASSERT_S(VideoCapture::FFMpegVideoClip(clip.AddressOf(), 640, 480, Encoding::H264, ContainerFormat::MP4));

    for(int32_t idx = 0; idx < 300; idx++)
    {
        std::string frame_path = dir + "/" + std::to_string(idx) + "_frame";
        std::string meta_path = dir + "/" + std::to_string(idx) + "_meta";

        ComPtr<IBuffer> h264_data;
        int64_t pts, dts, duration;
        ASSERT_S(LoadFrame(h264_data.AddressOf(), &pts, &dts, &duration, frame_path.c_str(), meta_path.c_str()));
        ASSERT_S(clip->WriteFrame(h264_data, pts, dts));
    }

    ASSERT_S(clip->Finalize());

    std::string output = dir + "/ffmpeg_video_clip.mp4";
    FILE* fptr = fopen(output.c_str(), "wb");
    fwrite(clip->Data(), clip->Size(), sizeof(uint8_t), fptr);
    fclose(fptr);

    ASSERT_S(ValidateVideoFile(output));
}

void VideoCaptureClass(const std::string& dir)
{
    HRESULT hr = S_OK;

    ComPtr<IVideoCapture> capture;
    ASSERT_S(VideoCapture::Create(capture.AddressOf(), 8000, 640, 480, Encoding::H264, ContainerFormat::MP4));

    // Add 240 frames (Key frames approx 1.6 seconds apart (50 frames))
    // First clip should be ~5 seconds, second clip should be ~3 seconds
    for(int32_t idx = 0; idx < 240; idx++)
    {
        std::string frame_path = dir + "/" + std::to_string(idx) + "_frame";
        std::string meta_path = dir + "/" + std::to_string(idx) + "_meta";

        ComPtr<IBuffer> h264_data;
        int64_t pts, dts, duration;
        ASSERT_S(LoadFrame(h264_data.AddressOf(), &pts, &dts, &duration, frame_path.c_str(), meta_path.c_str()));
        ASSERT_S(capture->AddFrame(h264_data, pts, dts, duration));
    }

    ComPtr<IVideoClipCollection> collection;
    ASSERT_S(capture->GenerateMultipleClips(collection.AddressOf(), 4000));
    ASSERT_EQ(collection->Count(), 2);

    for(int32_t idx = 0; idx < collection->Count(); idx++)
    {
        ComPtr<IVideoClip> clip;
        ASSERT_S(collection->Clip(clip.AddressOf(), idx));
        if(idx == 0)
        {
            ASSERT_TRUE(abs(clip->Duration() - 5000000000) < 500000000); // within a half a second of 5 seconds
        }
        else
        {
            ASSERT_TRUE(abs(clip->Duration() - 3000000000) < 500000000); // within a half a second of 3 seconds
        }

        TraceInfo("%lld", clip->Duration());
        std::string output = dir + "/videocapture_" + std::to_string(idx) + ".mp4";
        FILE* fptr = fopen(output.c_str(), "wb");
        fwrite(clip->Data(), clip->Size(), sizeof(uint8_t), fptr);
        fclose(fptr);

        // Earlier versions (3.4.11) of FFmpeg doesn't like the last video clip
        // Newer versions parse them just fine, unsure the issue.
        // Skip validating the last file for now
        if(idx < collection->Count() - 1)
        {
            ASSERT_S(ValidateVideoFile(output.c_str()));
        }
    }
}

void VideoCaptureClassAsync(const std::string& dir)
{
    HRESULT hr = S_OK;

    ComPtr<IVideoCapture> capture;
    ManualResetEvent clip_captured;

    // Capture a clip ~8 seconds long, chunk into ~2 second video clips with timestamp ~6.5 seconds at the center of the video
    ASSERT_S(VideoCapture::Create(capture.AddressOf(), 8000, 640, 480, Encoding::H264, ContainerFormat::MP4));
    ASSERT_S(capture->GenerateMultipleClipsAsync(2000, 3600006666666666, 0.5f, [&](IVideoClipCollection* collection, bool successful)
    {
        // Depending on how key frames are spaced this number could vary
        ASSERT_TRUE(collection->Count() > 1);
        ASSERT_TRUE(collection->Count() <= 4);
        for(int32_t idx = 0; idx < collection->Count(); idx++)
        {
            ComPtr<IVideoClip> clip;
            ASSERT_S(collection->Clip(clip.AddressOf(), idx));
            TraceInfo("%lld", clip->Duration());
            std::string output = dir + "/videocapture_async" + std::to_string(idx) + ".mp4";
            FILE* fptr = fopen(output.c_str(), "wb");
            fwrite(clip->Data(), clip->Size(), sizeof(uint8_t), fptr);
            fclose(fptr);

            // Earlier versions (3.4.11) of FFmpeg doesn't like the last video clip
            // Newer versions parse them just fine, unsure the issue.
            // Skip validating the last file for now
            if(idx < collection->Count() - 1)
            {
                ASSERT_S(ValidateVideoFile(output.c_str()));
            }

            clip_captured.Set();
        }
    }));

    // Add frames
    for(int32_t idx = 0; idx < 850 && clip_captured.WaitFor(0) == false; idx++)
    {
        std::string frame_path = dir + "/" + std::to_string(idx) + "_frame";
        std::string meta_path = dir + "/" + std::to_string(idx) + "_meta";

        ComPtr<IBuffer> h264_data;
        int64_t pts, dts, duration;
        ASSERT_S(LoadFrame(h264_data.AddressOf(), &pts, &dts, &duration, frame_path.c_str(), meta_path.c_str()));
        ASSERT_S(capture->AddFrame(h264_data, pts, dts, duration));
    }

    ASSERT_TRUE(clip_captured.WaitFor(3000));
}


void EvictionStrategyH264(const std::string& dir)
{
    HRESULT hr = S_OK;

    {
        // Test data key frames are approximately 1.65 seconds apart from each other (every 50 frames)
        // Set maximum length to 2 seconds
        ComPtr<IVideoCapture> capture;
        ASSERT_S(VideoCapture::Create(capture.AddressOf(), 2000, 640, 480, Encoding::H264, ContainerFormat::MP4));

        for(int32_t idx = 0; idx < 850; idx++)
        {
            std::string frame_path = dir + "/" + std::to_string(idx) + "_frame";
            std::string meta_path = dir + "/" + std::to_string(idx) + "_meta";

            ComPtr<IBuffer> h264_data;
            int64_t pts, dts, duration;
            ASSERT_S(LoadFrame(h264_data.AddressOf(), &pts, &dts, &duration, frame_path.c_str(), meta_path.c_str()));
            ASSERT_S(capture->AddFrame(h264_data, pts, dts, duration));
        }

        ComPtr<IVideoClip> clip;
        ASSERT_S(capture->GenerateClip(clip.AddressOf()));
        ASSERT_TRUE(clip->Duration() <= 2000000000);
    }

    {
        // Test data key frames are approximately 1.65 seconds apart from each other (every 50 frames)
        // Set maximum length to 1 seconds, something smaller than key frame distance.  Resulting clip should be larger than 1 second, but less <= key frame distance
        ComPtr<IVideoCapture> capture;
        ASSERT_S(VideoCapture::Create(capture.AddressOf(), 1000, 640, 480, Encoding::H264, ContainerFormat::MP4));

        // Don't end on a key frame becuase it will effectively clear out the video
        for(int32_t idx = 0; idx < 48; idx++)
        {
            std::string frame_path = dir + "/" + std::to_string(idx) + "_frame";
            std::string meta_path = dir + "/" + std::to_string(idx) + "_meta";

            ComPtr<IBuffer> h264_data;
            int64_t pts, dts, duration;
            ASSERT_S(LoadFrame(h264_data.AddressOf(), &pts, &dts, &duration, frame_path.c_str(), meta_path.c_str()));
            ASSERT_S(capture->AddFrame(h264_data, pts, dts, duration));
        }

        ComPtr<IVideoClip> clip;
        ASSERT_S(capture->GenerateClip(clip.AddressOf()));
        ASSERT_TRUE(clip->Duration() > 1000000000);
        ASSERT_TRUE(clip->Duration() <= 1650000000);
    }
}

TEST(VideoCapture, VideoCaptureTests)
{
    // Generate some test data
    HRESULT hr = S_OK;

    ComPtr<IApp> app = App::Create();
    std::string config =    "{                                                                  "
                            "    \"targets\": [                                                 "
                            "        {                                                          "
                            "            \"protocol\": \"file\",                                "
                            "            \"name\": \"output\",                                  "
                            "            \"file_options\": {}                                   "
                            "        }                                                          "
                            "    ],                                                             "
                            "    \"pipes\": [                                                   "
                            "        {                                                          "
                            "            \"message_id\": \"buf\",                               "
                            "            \"destinations\": [                                    "
                            "                {                                                  "
                            "                    \"target_name\": \"output\",                   "
                            "                    \"file_message_options\": {                    "
                            "                        \"directory\": \"./vc_test_data\",         "
                            "                        \"filename\": \"${count}_frame\"           "
                            "                    }                                              "
                            "                }                                                  "
                            "            ]                                                      "
                            "        },                                                         "
                            "        {                                                          "
                            "            \"message_id\": \"prop\",                              "
                            "            \"destinations\": [                                    "
                            "                {                                                  "
                            "                    \"target_name\": \"output\",                   "
                            "                    \"file_message_options\": {                    "
                            "                        \"directory\": \"./vc_test_data\",         "
                            "                        \"filename\": \"${count}_meta\"            "
                            "                    }                                              "
                            "                }                                                  "
                            "            ]                                                      "
                            "        }                                                          "
                            "    ]                                                              "
                            "}                                                                  ";

    SetEnvVar("GST_PLUGIN_PATH", BuildDirectory()+"/lib");
    ASSERT_S(GStreamer::Initialize());
    int32_t result = system("rm -r ./vc_test_data");

    ComPtr<IMessageBroker> broker;
    MessageBroker::SetDefaultConfig(config.c_str());
    ASSERT_S(MessageBroker::Create(broker.AddressOf(), app));
    ASSERT_S(broker->Initialize());

    int32_t frames_generated = 0;
    AutoResetEvent enough_frames;
    int32_t token = broker->Subscribe("buf", [&](IPayload* payload)
    {
        frames_generated++;
        if(frames_generated > 1250)
        {
            enough_frames.Set();
        }
    });
    ASSERT_S(token);

    ComPtr<IPipeline> pipeline;
    ASSERT_S(CreatePipeline(pipeline.AddressOf(), "id","videotestsrc ! capsfilter caps=video/x-raw,width=640,height=480,framerate=30/1 ! x264enc key-int-max=50 ! emlcapture buffer-message-id=buf buffer-properties=prop async=false interval=0 ! fakesink", app));
    ASSERT_S(pipeline->Start());

    // Generate some data
    ASSERT_TRUE(enough_frames.WaitFor(10000));

    ASSERT_S(pipeline->Stop());

    FFmpegVideoClip_H264_MP4("./vc_test_data");
    VideoCaptureClass("./vc_test_data");
    VideoCaptureClassAsync("./vc_test_data");
    EvictionStrategyH264("./vc_test_data");

    ASSERT_S(broker->Unsubscribe(token));
    result = system("rm -r ./vc_test_data");
}