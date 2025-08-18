#include <Panorama/app.h>
#include <Panorama/message_broker.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/gst.h>

using namespace Panorama;

// Tool that will capture frame data from the pipeline 
// To allow us to grab individual h264 frames + metadata for debugging
// If something isn't working with a particular data source
int main(int argc, char* argv[])
{
    try
    {
        HRESULT hr = S_OK;
        ADD_CONSOLE_TRACE;

        ComPtr<IApp> app = App::CreateWithArgs(argc, argv);
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
                                "                        \"directory\": \"./\",                     "
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
                                "                        \"directory\": \"./\",                     "
                                "                        \"filename\": \"${count}_meta\"            "
                                "                    }                                              "
                                "                }                                                  "
                                "            ]                                                      "
                                "        }                                                          "
                                "    ]                                                              "
                                "}                                                                  ";

        CHECKHR(GStreamer::Initialize());

        ComPtr<IMessageBroker> broker;
        MessageBroker::SetDefaultConfig(config.c_str());
        CHECKHR(MessageBroker::Create(broker.AddressOf(), app));
        CHECKHR(broker->Initialize());

        ComPtr<IStringProperty> src;
        CHECKHR_MSG(app->GetStringProperty(src.AddressOf(), "src"), "Usage: VideoCaptureTool --src <upstream gstreamer pipeline> (e.g. rtspsrc location=... ! rtph264depay)");

        std::string def = std::string(src->Get()) + " ! emlcapture buffer-message-id=buf buffer-properties=prop async=false interval=0 ! fakesink";
        ComPtr<IPipeline> pipeline;
        CHECKHR(CreatePipeline(pipeline.AddressOf(), "id", def.c_str(), app));
        CHECKHR(pipeline->Start());

        TraceInfo("Hit any key to stop capture");
        int user_input = getchar();
        pipeline->Stop();
    }
    catch(const std::exception& e)
    {
        TraceError("Unexpected error: %s", e.what());
    }
}