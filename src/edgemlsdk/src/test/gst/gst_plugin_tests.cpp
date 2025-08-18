#include <Panorama/comptr.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/gst.h>
#include <Panorama/eventing.h>
#include <Panorama/app.h>
#include <Panorama/properties.h>
#include <Panorama/aws.h>
#include <Panorama/message_broker.h>
#include <Panorama/videocapture.h>
#include <Panorama/mlops.h>
#include <misc.h>
#include <env_vars.h>
#include <thread>

#define GTEST_SETUP
#include <TestUtils.h>
#include <filesystem_safe.h>

using namespace Panorama;

class GlobalSetup : public ::testing::Environment 
{
public:
    void SetUp() override 
    {
        SetEnvVar("GST_PLUGIN_PATH", BuildDirectory()+"/lib");
        GStreamer::Initialize();
    }

    void TearDown() override 
    {
        GStreamer::Shutdown();
    }
};

::testing::Environment* CreateGlobalSetup()
{
    return new GlobalSetup();
}

int* counter;
static GstPadProbeReturn GetProbe(GstPad *pad, GstPadProbeInfo *info, gpointer user_data) 
{
    if(counter != nullptr)
    {
        (*counter)++;
    }

    if(user_data != nullptr)
    {
        reinterpret_cast<ManualResetEvent*>(user_data)->Set();
    }

    return GST_PAD_PROBE_OK;  // Allow data to flow.
}

TEST(GstPluginTests, gate)
{
    HRESULT hr = S_OK;

    ComPtr<IApp> app = App::Create();
    ComPtr<IPipeline> pipeline;
    ASSERT_S(CreatePipeline(pipeline.AddressOf(), "id", "videotestsrc ! gate open=false name=g0 subscription-id=c0 ! gate open=false name=g1 subscription-id=c1 ! fakesink", app));

    // Register probes
    ManualResetEvent g0_signal, g1_signal;
    GstElement* gst_pipeline = pipeline->Element();
    GstElement* g0 = gst_bin_get_by_name(GST_BIN(gst_pipeline), "g0");
    GstPad *srcpad0 = gst_element_get_static_pad(g0, "src");
    gst_pad_add_probe(srcpad0, GST_PAD_PROBE_TYPE_BUFFER, GetProbe, &g0_signal, NULL);

    GstElement* g1 = gst_bin_get_by_name(GST_BIN(gst_pipeline), "g1");
    GstPad *srcpad1 = gst_element_get_static_pad(g1, "src");
    gst_pad_add_probe(srcpad1, GST_PAD_PROBE_TYPE_BUFFER, GetProbe, &g1_signal, NULL);

    ComPtr<ICredentialProvider> credentials;
    credentials = Panorama_Aws::DefaultCredentialProvider();
    ComPtr<IMessageBroker> broker;
    ASSERT_S(MessageBroker::Create(broker.AddressOf(), credentials));
    ASSERT_S(broker->Initialize());
    
    AutoResetEvent playing_state;
    pipeline->OnStateChange([&](IPipeline* sender, int32_t old_state, int32_t new_state)
    {
        if(new_state == GST_STATE_PLAYING)
        {
            playing_state.Set();
        }
    });

    ASSERT_S(pipeline->Start());
    

    // Signal through GStreamer APIs
    {
        // Set the property open to true on g0, but not g1
        g_object_set(G_OBJECT(g0), "open", true, NULL);
        ASSERT_TRUE(g0_signal.WaitFor(250));
        ASSERT_FALSE(g1_signal.WaitFor(250));

        // Set the property open to true on g1
        g_object_set(G_OBJECT(g1), "open", true, NULL);
        ASSERT_TRUE(g0_signal.WaitFor(250));
        ASSERT_TRUE(g1_signal.WaitFor(250));

        // First frames have made it through, pipeline should have transitioned fully to playing at this point
        ASSERT_TRUE(playing_state.WaitFor(3000));

        // Close both gates and allow a set number of frames through gate0
        g_object_set(G_OBJECT(g0), "open", false, NULL);
        g_object_set(G_OBJECT(g1), "open", false, NULL);
        g0_signal.Reset();
        g1_signal.Reset();
        ASSERT_FALSE(g0_signal.WaitFor(250));
        ASSERT_FALSE(g1_signal.WaitFor(250));

        int counter0 = 0;
        counter = &counter0;
        g_object_set(G_OBJECT(g0), "numframes", 10, NULL);
        ThreadSleep(500);
        ASSERT_EQ(counter0, 10);
        ASSERT_TRUE(g0_signal.WaitFor(0));
        ASSERT_FALSE(g1_signal.WaitFor(0));
        counter = nullptr;
    }

    // Signal through event broker
    {
        // open both gates
        nlohmann::json command;
        command["open"] = true;
        ASSERT_S(broker->Publish("c0", command.dump().c_str()));
        ASSERT_S(broker->Publish("c1", command.dump().c_str()));
        g0_signal.Reset();
        g1_signal.Reset();
        ASSERT_TRUE(g0_signal.WaitFor(250));
        ASSERT_TRUE(g1_signal.WaitFor(250));

        // close gate1
        command["open"] = false;
        ASSERT_S(broker->Publish("c1", command.dump().c_str()));
        g0_signal.Reset();
        g1_signal.Reset();
        ASSERT_TRUE(g0_signal.WaitFor(250));
        ASSERT_FALSE(g1_signal.WaitFor(250));

         // Close both gates and allow a set number of frames through gate0
        ASSERT_S(broker->Publish("c0", command.dump().c_str()));
        g0_signal.Reset();
        g1_signal.Reset();
        ASSERT_FALSE(g0_signal.WaitFor(250));
        ASSERT_FALSE(g1_signal.WaitFor(250));

        int counter0 = 0;
        counter = &counter0;
        command["num_frames"] = 10;
        ASSERT_S(broker->Publish("c0", command.dump().c_str()));
        ThreadSleep(500);
        ASSERT_EQ(counter0, 10);
        ASSERT_TRUE(g0_signal.WaitFor(0));
        ASSERT_FALSE(g1_signal.WaitFor(0));
        counter = nullptr;
    }

    pipeline->Stop();

    gst_object_unref(g0);
    gst_object_unref(g1);
    gst_object_unref(srcpad0);
    gst_object_unref(srcpad1);
}

TEST(GstPluginTests, gate2)
{
    HRESULT hr = S_OK;

    ComPtr<IApp> app = App::Create();
   
    ComPtr<IMessageBroker> broker;
    ASSERT_S(MessageBroker::Create(broker.AddressOf()));

    ManualResetEvent cap1, cap2;
    broker->Subscribe("cap1", [&](IPayload* payload)
    {
        cap1.Set();
    });

    broker->Subscribe("cap2", [&](IPayload* payload)
    {
        cap2.Set();
    });

    {
        ComPtr<IPipeline> pipeline;
        ASSERT_S(CreatePipeline(pipeline.AddressOf(), "id", "videotestsrc ! gate open=false go-to=incorrect ! emlcapture buffer-message-id=cap1 interval=0 name=goto ! emlcapture buffer-message-id=cap2 interval=0 ! fakesink", app));
        ASSERT_F(pipeline->Start());
    }

    {
        cap1.Reset();
        cap2.Reset();
        ComPtr<IPipeline> pipeline;
        ASSERT_S(CreatePipeline(pipeline.AddressOf(), "id", "videotestsrc ! gate open=false go-to=goto ! emlcapture buffer-message-id=cap1 interval=0 name=goto ! emlcapture buffer-message-id=cap2 interval=0 ! fakesink", app));
        ASSERT_S(pipeline->Start());
        ASSERT_TRUE(cap2.WaitFor(3000));
        ASSERT_FALSE(cap1.WaitFor(0));
    }

    {
        cap1.Reset();
        cap2.Reset();
        ComPtr<IPipeline> pipeline;
        ASSERT_S(CreatePipeline(pipeline.AddressOf(), "id", "videotestsrc ! gate open=true go-to=goto ! emlcapture buffer-message-id=cap1 interval=0 name=goto ! emlcapture buffer-message-id=cap2 interval=0 ! fakesink", app));
        ASSERT_S(pipeline->Start());
        ASSERT_TRUE(cap2.WaitFor(3000));
        ASSERT_TRUE(cap1.WaitFor(0));
    }
}

static void need_data(GstElement *appsrc, guint unused_size, gpointer user_data) 
{
    HRESULT hr = S_OK;

    static GstClockTime timestamp = 0;
    GstBuffer *buffer;
    guint size;
    GstFlowReturn ret;
    GstMapInfo map;

    size = 320 * 240 * 3;  
    buffer = gst_buffer_new_allocate (NULL, size, NULL);

    ComPtr<IPayload> payload;
    ASSERT_S(MessageBroker::CreatePayload(payload.AddressOf(), "hello world"));
    ASSERT_S(GStreamer::AddPayloadToBuffer(payload, buffer, "test_1"));

    gst_buffer_map(buffer, &map, GST_MAP_WRITE);
    // fill the GstBuffer
    memset(map.data, 128, size);
    GST_BUFFER_PTS(buffer) = GST_CLOCK_TIME_NONE;
    GST_BUFFER_DURATION(buffer) = GST_CLOCK_TIME_NONE;
    gst_buffer_unmap(buffer, &map);

    g_signal_emit_by_name(appsrc, "push-buffer", buffer, &ret);
    gst_buffer_unref(buffer);
}

TEST(GstPluginTests, emlcapture)
{
    HRESULT hr = S_OK;

    ComPtr<IMessageBroker> broker;
    ASSERT_S(MessageBroker::Create(broker.AddressOf()));
    ASSERT_S(broker->Initialize());

    AutoResetEvent buffer_published;
    ASSERT_S(broker->Subscribe("buffer-id", [&](IPayload* payload)
    {
        buffer_published.Set();
    }));

    ComPtr<IApp> app = App::Create();

    {
        // On demand capture for buffer
        ComPtr<IPipeline> pipeline;
        ASSERT_S(CreatePipeline(pipeline.AddressOf(), "id", "videotestsrc ! emlcapture buffer-message-id=buffer-id subscription-id=capture ! fakesink", app));
        ASSERT_S(pipeline->Start());

        ASSERT_S(broker->Publish("capture", "{}"));
        ASSERT_TRUE(buffer_published.WaitFor(250));

        // Hanging onto this tests in case we want to enable the below feature.  Will remove when decision to not have this feature is decided.
        // Publish a request, buffer should be published to buffer-id
        // ASSERT_S(broker->Publish("capture", "{\"buffer-message-id\":\"buffer-id\"}"));
        // ASSERT_TRUE(buffer_published.WaitFor(250));

        // // Subsequent calls should re-use value for previous capture requests
        // ASSERT_S(broker->Publish("capture", "{}"));
        // ASSERT_TRUE(buffer_published.WaitFor(250));

        // // Publish a request, buffer should not be captured since buffer-message-id is empty
        // ASSERT_S(broker->Publish("capture", "{\"buffer-message-id\":\"\"}"));
        // ASSERT_FALSE(buffer_published.WaitFor(250));
    }

    {
        // Continual capturing of buffer id
        ComPtr<IPipeline> pipeline;
        ASSERT_S(CreatePipeline(pipeline.AddressOf(), "id", "videotestsrc ! emlcapture buffer-message-id=buffer-id interval=0 ! fakesink", app));
        ASSERT_S(pipeline->Start());

        ASSERT_TRUE(buffer_published.WaitFor(30));
        ASSERT_TRUE(buffer_published.WaitFor(30));
        ASSERT_TRUE(buffer_published.WaitFor(30));
        ASSERT_TRUE(buffer_published.WaitFor(30));
        ASSERT_TRUE(buffer_published.WaitFor(30));
        ASSERT_TRUE(buffer_published.WaitFor(30));
    }

    {
        // Periodic capturing of buffer id
        ComPtr<IPipeline> pipeline;
        ASSERT_S(CreatePipeline(pipeline.AddressOf(), "id", "videotestsrc ! emlcapture buffer-message-id=buffer-id interval=100 async=false ! fakesink", app));
        Timestamp start = NowAsTimestamp();
        ASSERT_S(pipeline->Start());

        // Possible that a buffer was published after last buffer_published was waiting on in previous test
        // Making the first time check fail since buffer_published.waitfor will return instantly
        // So, just wait once in case that happened
        ASSERT_TRUE(buffer_published.WaitFor(125));
        ASSERT_TRUE(buffer_published.WaitFor(125));

        ASSERT_TRUE(TimestampToMilliseconds(NowAsTimestamp() - start) >= 90);
        start = NowAsTimestamp();

        ASSERT_TRUE(buffer_published.WaitFor(125));
        ASSERT_TRUE(TimestampToMilliseconds(NowAsTimestamp() - start) >= 90);
        start = NowAsTimestamp();

        ASSERT_TRUE(buffer_published.WaitFor(125));
        ASSERT_TRUE(TimestampToMilliseconds(NowAsTimestamp() - start) >= 90);
        start = NowAsTimestamp();

        ASSERT_TRUE(buffer_published.WaitFor(125));
        ASSERT_TRUE(TimestampToMilliseconds(NowAsTimestamp() - start) >= 90);
        start = NowAsTimestamp();

        ASSERT_TRUE(buffer_published.WaitFor(125));
        ASSERT_TRUE(TimestampToMilliseconds(NowAsTimestamp() - start) >= 90);
        start = NowAsTimestamp();

        ASSERT_TRUE(buffer_published.WaitFor(125));
        ASSERT_TRUE(TimestampToMilliseconds(NowAsTimestamp() - start) >= 90);
        start = NowAsTimestamp();
    }

    {
        AutoResetEvent meta_published;
        ASSERT_S(broker->Subscribe("test_message", [&](IPayload* payload)
        {
            ASSERT_TRUE(strcmp(payload->SerializeAsString(), "hello world") == 0);
            meta_published.Set();
        }));
        int32_t token = hr;

        ComPtr<IPipeline> pipeline;
        ASSERT_S(CreatePipeline(pipeline.AddressOf(), "id", "appsrc name=src ! emlcapture meta=test_1:test_message interval=0 ! fakesink", app));

        GstElement* bin = pipeline->Element();
        GstElement* appsrc = gst_bin_get_by_name(GST_BIN(bin), "src");
        ASSERT_TRUE(appsrc != nullptr);

        g_object_set(G_OBJECT(appsrc), "caps",
                 gst_caps_new_simple("video/x-raw",
                                     "format", G_TYPE_STRING, "RGB",
                                     "width", G_TYPE_INT, 320,
                                     "height", G_TYPE_INT, 240,
                                     "framerate", GST_TYPE_FRACTION, 30, 1,
                                     NULL), NULL);
        g_signal_connect(appsrc, "need-data", G_CALLBACK(need_data), nullptr);
        
        ASSERT_S(pipeline->Start());
        ASSERT_TRUE(meta_published.WaitFor(1000));

        broker->Unsubscribe(token);
    }
}

TEST(GstPluginTests, emlfilesrc)
{
    HRESULT hr = S_OK;

    std::string file_path = BuildDirectory() + "/file_src_plugin_test.txt";
    FILE* fptr = fopen(file_path.c_str(), "w");
    fprintf(fptr, "Hello World");
    fclose(fptr);

    ComPtr<IApp> app = App::Create();

    ComPtr<IMessageBroker> broker;
    ASSERT_S(MessageBroker::Create(broker.AddressOf()));
    ASSERT_S(broker->Initialize());

    ComPtr<IPipeline> pipeline;
    ASSERT_S(CreatePipeline(pipeline.AddressOf(), "id", "emlfilesrc subscription-id=trigger ! emlcapture buffer-message-id=image interval=0 ! fakesink", app));
    ASSERT_S(pipeline->Start());

    ManualResetEvent image_received;
    ASSERT_S(broker->Subscribe("image", [&](IPayload* payload)
    {
        image_received.Set();
        ASSERT_TRUE(strcmp(payload->CorrelationId(), "my-correlation-id") == 0);
        ASSERT_TRUE(strcmp(payload->SerializeAsString(), "Hello World") == 0);
    }));

    nlohmann::json trigger;
    trigger["correlation-id"] = "my-correlation-id";
    trigger["file-path"] = file_path;

    ComPtr<IPayload> payload;
    ASSERT_S(MessageBroker::CreatePayload(payload.AddressOf(), trigger.dump().c_str()));
    broker->Publish("trigger", payload);
    ASSERT_TRUE(image_received.WaitFor(3000));
}

TEST(GstPluginTests, MqttUnsubscribe)
{
    // Test to excercise this scenario:
    // Plugin unsubscribing from message broker when protocol client is mqtt was causing a hang
    // https://taskei.amazon.dev/tasks/V1159330545
    HRESULT hr = S_OK;
    std::string config =    "{                                                                                  "
                            "    \"targets\": [                                                                 "
                            "        {                                                                          "
                            "            \"protocol\": \"mqtt\",                                                "
                            "            \"name\": \"sample-mqtt\",                                             "
                            "            \"mqtt_options\": {                                                    "
                            "                \"endpoint\": \"a5h6960s3xow6-ats.iot.us-west-2.amazonaws.com\",   "
                            "                \"region\": \"us-west-2\"                                          "
                            "            },                                                                     "
                            "            \"mqtt_subscriptions\": [                                              "
                            "                {                                                                  "
                            "                    \"subscription_id\": \"sub-id\",                               "
                            "                    \"topic\": \"test/mqtt/unsubcribe\"                            "
                            "                }                                                                  "
                            "            ]                                                                      "
                            "        }                                                                          "
                            "    ],                                                                             "
                            "    \"pipes\": [                                                                   "
                            "        {                                                                          "
                            "            \"message_id\": \"test-message\",                                      "
                            "            \"destinations\": [                                                    "
                            "                {                                                                  "
                            "                    \"target_name\": \"sample-mqtt\",                              "
                            "                    \"mqtt_message_options\": {                                    "
                            "                        \"topic\": \"test/mqtt/unsubcribe\"                       "
                            "                    }                                                              "
                            "                }                                                                  "
                            "            ]                                                                      "
                            "        }                                                                          "
                            "    ]                                                                              "
                            "}                                                                                  ";

    MessageBroker::SetDefaultConfig(config.c_str());
    ComPtr<IApp> app = App::Create();

    ComPtr<IMessageBroker> broker;
    ASSERT_S(MessageBroker::Create(broker.AddressOf(), app));
    ASSERT_S(broker->Initialize());

    ComPtr<IPipeline> pipeline;
    ASSERT_S(CreatePipeline(pipeline.AddressOf(), "id", "videotestsrc ! gate subscription-id=sub-id open=true ! fakesink", app));

    // Start the pipeline
    ASSERT_S(pipeline->Start());

    // Send a payload
    ComPtr<IPayload> payload;
    ASSERT_S(MessageBroker::CreatePayload(payload.AddressOf(), "{\"open\": true}"));
    ASSERT_S(broker->Publish("test-message", payload));

    // Restart the pipeline
    ASSERT_S(pipeline->Restart());
    ASSERT_S(pipeline->Stop());
}

TEST(GstPluginTests, emltriton)
{
    HRESULT hr = S_OK;
    ComPtr<IApp> app = App::Create();

    ComPtr<IMessageBroker> broker;
    MessageBroker::SetDefaultConfig("");
    ASSERT_S(MessageBroker::Create(broker.AddressOf()));
    ASSERT_S(broker->Initialize());
    std::string output_name = "output_0";
    std::string modelDirectory = BuildDirectory() + "/bin/model_repo";
    ComPtr<IInferenceServer> t_server;
    ASSERT_S(MLOps::TritonInferenceServer(t_server.AddressOf(), modelDirectory.c_str(), TRITON_INSTALL_DIR));
    t_server->LoadModel("dynamic_model_2");
    t_server->LoadModel("test_model");
    t_server->LoadModel("test_model2");
    // Wait for models to load.
    std::this_thread::sleep_for(std::chrono::milliseconds(3000));
    t_server->LoadModel("test_model");
    AutoResetEvent inference_results_received;
    broker->Subscribe("results", [&](IPayload *payload)
    {
        inference_results_received.Set();
    });
    {
        ComPtr<IPipeline> pipeline;
        std::string pipeline_string =
            "videotestsrc num-buffers=1 ! capsfilter caps=video/x-raw,width=32,height=32,format=RGB ! "
            "emltriton model=dynamic_model_2 model-repo=" + modelDirectory + " server-path=" + TRITON_INSTALL_DIR + " unique=true ! "
            "emlcapture async=false interval=0 meta=triton_inference_"+  output_name +":results ! "
            "fakesink";

        ASSERT_S(CreatePipeline(pipeline.AddressOf(), "id", pipeline_string.c_str(), app));
        // Should fail since model is not loaded.
        ASSERT_F(pipeline->Start());
    }
    {
        ComPtr<IPipeline> pipeline;
        std::string pipeline_string = 
            "videotestsrc num-buffers=1 ! capsfilter caps=video/x-raw,width=32,height=32,format=RGB ! "
            "emltriton model=dynamic_model_2 model-repo=" + modelDirectory + " server-path=" + TRITON_INSTALL_DIR + " ! "
            "emlcapture async=false interval=0 meta=triton_inference_"+  output_name +":results ! "
            "fakesink";

        ASSERT_S(CreatePipeline(pipeline.AddressOf(), "id", pipeline_string.c_str(), app));
        ASSERT_S(pipeline->Start());

        // Refcount should be 3 because the pipeline and the t_server
        ASSERT_EQ(t_server->RefCount(), 3);

        ASSERT_TRUE(inference_results_received.WaitFor(3000));
        ASSERT_S(pipeline->Stop());
    }

    {
        ComPtr<IPipeline> pipeline;
        std::string pipeline_string = 
            "videotestsrc num-buffers=1 ! capsfilter caps=video/x-raw,width=32,height=32,format=RGB ! "
            "emltriton model=dynamic_model_2 model-repo=" + modelDirectory + " server-path=" + TRITON_INSTALL_DIR + " ! "
            "emlcapture async=false interval=0 meta=triton_inference_"+  output_name + ":results ! "
            "fakesink";

        ASSERT_S(CreatePipeline(pipeline.AddressOf(), "id", pipeline_string.c_str(), app));
        ASSERT_S(pipeline->Start());

        // Pipeline was created with unique=false (default).  So creating another server (default unique=false) with same model/install
        // Should result in a IInferenceServer with 4 references (reference here, gst element, and the initial created object and the main t_server)
        ComPtr<IInferenceServer> server;
        ASSERT_S(MLOps::TritonInferenceServer(server.AddressOf(), modelDirectory.c_str(), TRITON_INSTALL_DIR));
        ASSERT_EQ(server->RefCount(), 4);

        ASSERT_S(pipeline->Stop());
    }

    {
        ComPtr<IPipeline> pipeline;
        std::string pipeline_string = 
            "videotestsrc ! capsfilter caps=video/x-raw,width=32,height=32,format=RGB ! "
            "emltriton model=test_model model-repo=" + modelDirectory + " server-path=" + TRITON_INSTALL_DIR + " ! "
            "emlcapture async=false interval=0 meta=triton_inference_"+  output_name +":results ! "
            "fakesink";

        ASSERT_S(CreatePipeline(pipeline.AddressOf(), "id", pipeline_string.c_str(), app));
        ASSERT_S(pipeline->Start());

        ManualResetEvent received_error;
        pipeline->OnError([&](IPipeline* sender, IPipelineError* error)
        {
            received_error.Set();
        });

        // Input buffer 32x32x1 does not match the input tensor size, so pipeline should fail
        ASSERT_TRUE(received_error.WaitFor(3000));
        ASSERT_S(pipeline->Stop());
    }

    {
        ComPtr<IPipeline> pipeline;
        std::string pipeline_string = 
            "videotestsrc ! capsfilter caps=video/x-raw,width=32,height=32,format=RGB ! "
            "emltriton model=dynamic_model_2 metadata=foo model-repo=" + modelDirectory + " server-path=" + TRITON_INSTALL_DIR + " ! "
            "emlcapture async=false interval=0 meta=triton_inference_"+  output_name +":results ! "
            "fakesink";

        ASSERT_S(CreatePipeline(pipeline.AddressOf(), "id", pipeline_string.c_str(), app));

        // model 'dynamic_model_2' does not have input named METADATA but trying to set metadata in pipeline, should fail to start
        ASSERT_F(pipeline->Start());
    }

    {
        ComPtr<IPipeline> pipeline;
        std::string pipeline_string = 
            "videotestsrc ! capsfilter caps=video/x-raw,width=32,height=32,format=RGB ! "
            "emltriton model=test_model2 metadata=foo model-repo=" + modelDirectory + " server-path=" + TRITON_INSTALL_DIR + " ! "
            "emlcapture async=false interval=0 meta=triton_inference_"+  output_name +":results ! "
            "fakesink";

        ASSERT_S(CreatePipeline(pipeline.AddressOf(), "id", pipeline_string.c_str(), app));

        // model 'test-model2' has input named METADATA
        ASSERT_S(pipeline->Start());
    }

    {
        ComPtr<IPipeline> pipeline;
        std::string pipeline_string =
            "videotestsrc ! capsfilter caps=video/x-raw,width=32,height=32,format=RGB ! "
            "emltriton model=dynamic_model_2 model-repo=" + modelDirectory + " server-path=" + TRITON_INSTALL_DIR + " ! "
            "emlcapture async=false interval=0 meta=triton_inference_"+  output_name +":results ! "
            "fakesink";

        ASSERT_S(CreatePipeline(pipeline.AddressOf(), "id", pipeline_string.c_str(), app));

        // model 'dynamic_model' has dynamic inputs, should run
        ASSERT_S(pipeline->Start());
        ASSERT_TRUE(inference_results_received.WaitFor(3000));
        ASSERT_S(pipeline->Stop());
    }

    MLOps::ReleaseTritonServers();
}

HRESULT ValidateVideoFile(const std::string& filepath)
{
    HRESULT hr = S_OK;
    std::string command = "ffmpeg -i " + filepath + " -f null -";
    CHECKIF(system(command.c_str()) != 0, E_FAIL);
    return hr;
}

// TEST(GstPluginTests, emlvideocapture)
// {
//     HRESULT hr = S_OK;
//     ComPtr<IApp> app = App::Create();

//     ComPtr<IMessageBroker> broker;
//     ASSERT_S(MessageBroker::Create(broker.AddressOf(), app));
//     ASSERT_S(broker->Initialize());

//     AutoResetEvent video_received;

//     ComPtr<IBatchPayload> batch;
//     broker->Subscribe("video", [&](IPayload* payload)
//     {
//         batch = ComPtr<IPayload>(payload).QueryInterface<IBatchPayload>();
//         video_received.Set();
//     });

//     ComPtr<IPipeline> pipeline;
//     ASSERT_S(CreatePipeline(pipeline.AddressOf(), "id", "videotestsrc pattern=ball ! capsfilter caps=video/x-raw,width=640,height=480,framerate=30/1 ! x264enc tune=zerolatency key-int-max=50 ! emlvideocapture message-id=video subscription-id=test max-length=10000 ! fakesink", app));

//     GstClock *system_clock = gst_system_clock_obtain();
//     ASSERT_S(pipeline->Start());

//     // Give the pipeline a chance to acquire some frames
//     ThreadSleep(3000);

//     {
//         // Capture a video with current time at the beginning of the video clip
//         GstClockTime current_time = gst_clock_get_time(system_clock);
//         nlohmann::json command;
//         command["timestamp"] = current_time;
//         command["pos"] = 0.0f;
//         broker->Publish("test", command.dump().c_str());
//         ASSERT_TRUE(video_received.WaitFor(3000));
//         ASSERT_TRUE(batch != nullptr);
//         ASSERT_EQ(batch->Count(), 1);
//         for(int32_t idx = 0; idx < batch->Count(); idx++)
//         {
//             ComPtr<IPayload> payload;
//             ASSERT_S(batch->Payload(payload.AddressOf(), idx));

//             ComPtr<IVideoPayload> videoPayload;
//             videoPayload = payload.QueryInterface<IVideoPayload>();
//             ASSERT_NE(videoPayload, nullptr);

//             // Test SerializeVideoClip, in addition to Serialize.
//             ComPtr<IVideoClip> clip;
//             ASSERT_S(videoPayload->SerializeVideoClip(clip.AddressOf()));

//             std::string output = "./"+std::to_string(videoPayload->Timestamp()) + ".mp4";
//             FILE* fptr = fopen(output.c_str(), "wb");
//             fwrite(clip->Data(), clip->Size(), sizeof(uint8_t), fptr);
//             fclose(fptr);
//             ASSERT_S(ValidateVideoFile(output.c_str()));
//             std::string rm = "rm " + output;
//             int32_t result = system(rm.c_str());
//         }
//     }

//     {
//         // Capture a video with current time at the beginning of the video clip, but split into several clips ~2 seconds each
//         GstClockTime current_time = gst_clock_get_time(system_clock);
//         nlohmann::json command;
//         command["timestamp"] = current_time;
//         command["pos"] = 0.0f;
//         command["clip_length"] = 2000;
//         broker->Publish("test", command.dump().c_str());
//         ASSERT_TRUE(video_received.WaitFor(3000));
//         ASSERT_TRUE(batch != nullptr);

//         // Only expect 3 since key-frame distance is 50 (~1.66 seconds) so each clip, 
//         // even though specified for 2 seconds will be closer to 3.32, except the last one
//         // Additionally the video gets truncated by 1.66 seconds when hits 10 seconds so only
//         // way we get 4 is if we request the video in the last 0.04 seconds (but it's possible)
//         ASSERT_TRUE(batch->Count() == 3 || batch->Count() == 4);
//         int64_t duration = 0;
//         for(int32_t idx = 0; idx < batch->Count(); idx++)
//         {
//             ComPtr<IPayload> payload;
//             ASSERT_S(batch->Payload(payload.AddressOf(), idx));

//             ComPtr<IBuffer> data;
//             ASSERT_S(payload->Serialize(data.AddressOf()));
//             // Test Serialize, in addition to SerializeVideoClip
//             ComPtr<IVideoClip> clip = data.QueryInterface<IVideoClip>();
//             ASSERT_TRUE(clip != nullptr);
//             duration += clip->Duration();
//             std::string output = "./"+std::to_string(payload->Timestamp()) + ".mp4";
//             FILE* fptr = fopen(output.c_str(), "wb");
//             fwrite(data->Data(), data->Size(), sizeof(uint8_t), fptr);
//             fclose(fptr);

//             // Earlier versions (3.4.11) of FFmpeg doesn't like the last video clip
//             // Newer versions parse them just fine, unsure the issue.
//             // Skip validating the last file for now
//             if(idx < batch->Count() - 1)
//             {
//                 ASSERT_S(ValidateVideoFile(output.c_str()));
//             }

//             std::string rm = "rm " + output;
//             int32_t result = system(rm.c_str());
//         }

//         ASSERT_TRUE(10000 - duration <= 1.7);
//     }

//     pipeline->Stop();
//     gst_object_unref(system_clock);
// }