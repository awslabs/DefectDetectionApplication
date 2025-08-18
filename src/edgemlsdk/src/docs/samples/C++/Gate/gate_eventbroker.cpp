#include <iostream>
#include <string>

#include <Panorama/gst.h>
#include <Panorama/edge/event_broker.h>
#include <Panorama/aws.h>
using namespace Panorama;

int main(int argc, char* argv[])
{
    /*
    Starts a simple pipeline with a gate.  Shows code to control the gate through the event broker
    If EVENT_BROKER_SCHEMA_FILE is set then the gate can also be controlled from the targets specified in the schema file
    */
    ADD_CONSOLE_TRACE;
    HRESULT hr = S_OK;
    CHECKHR(GStreamer::Initialize());

    // Create the pipeline
    ComPtr<IApp> app = App::Create();
    CHECKNULL(app, E_FAIL);

    ComPtr<IPipeline> pipeline;
    CHECKHR(CreatePipeline(pipeline.AddressOf(), "id", "videotestsrc ! gate open=false name=g1 command=gate_command ! ximagesink", app));

    // Create the event broker
    ComPtr<IEventBroker> broker;
    CHECKHR(EventBroker::Create(broker.AddressOf(), nullptr, nullptr));
    CHECKHR(broker->Initialize());

    // Start the pipeline
    CHECKHR(pipeline->Start());

    int num_frames = 0;
    do
    {
        std::cout << "Enter number of frames to pass through gate (-1 to exit): ";
        std::cin >> num_frames;

        if(num_frames >= 0)
        {
            std::stringstream payload;
            payload << "{ \"open\": false, \"num_frames\":" << num_frames << "}";
            broker->PublishCommandFromString("gate_command", payload.str().c_str());
        }
    } while (num_frames >= 0);

    CHECKHR(pipeline->Stop());
    CHECKHR(GStreamer::Shutdown());
    return 0;
}