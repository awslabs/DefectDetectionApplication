#include <Panorama/message_broker.h>
#include <Panorama/gst.h>

using namespace Panorama;

int main()
{
    ADD_CONSOLE_TRACE;

    // Setting to verbose so console outputs data capture events
    Tracer::SetTraceLevel(TraceLevel::Verbose);
    HRESULT hr = S_OK;

    // Routes message published to capture-id to a file sink.
    // Messages will be saved to ./${timestamp}-image.jpg
    std::string config = 
    "{                                                                 "
    "    \"targets\": [                                                "
    "        {                                                         "
    "            \"protocol\": \"file\",                               "
    "            \"name\": \"sink\",                                   "
    "            \"file_options\": {}                                  "
    "        }                                                         "
    "    ],                                                            "
    "    \"pipes\": [                                                  "
    "        {                                                         "
    "            \"message_id\": \"capture-id\",                       "
    "            \"destinations\": [                                   "
    "                {                                                 "
    "                    \"target_name\": \"sink\",                    "
    "                    \"file_message_options\": {                   "
    "                        \"directory\": \"./\",                    "
    "                        \"filename\": \"${timestamp}-image.jpg\"  "
    "                    }                                             "
    "                }                                                 "
    "            ]                                                     "
    "        }                                                         "
    "    ]                                                             "
    "}                                                                 ";

    // Initialize GStreamer
    CHECKHR(GStreamer::Initialize());

    // Set the default config before creating the pipeline
    // emlcapture will load the default config if it's set.
    MessageBroker::SetDefaultConfig(config.c_str());

    // Create the message broker
    ComPtr<IMessageBroker> broker;
    CHECKHR(MessageBroker::Create(broker.AddressOf()));
    CHECKHR(broker->Initialize());

    // Create the GStreamer pipeline that will capture a jpg image once every 3 seconds or on demand
    ComPtr<IApp> app = App::Create();
    ComPtr<IPipeline> pipeline;
    CHECKHR(CreatePipeline(pipeline.AddressOf(), "id", "videotestsrc ! jpegenc ! emlcapture subscription-id=capture interval=3000 buffer-message-id=capture-id ! fakesink", app));
    CHECKHR(pipeline->Start());

    TraceInfo("Hit 'c' to capture or any other key to exit");
    char key;

    do
    {
        std::cin >> key;
        if(key == 'c')
        {
            // Publish a message to 'capture'.  emlcapture was configured to listen to this through the 'subscription-id' parameter
            // Message can be empty JSON because buffer-message-id was set as a element property
            CHECKHR(broker->Publish("capture", "{}"));
        }
        else
        {
            break;
        }
    }while(true);

    CHECKHR(pipeline->Stop());
    return hr;
}