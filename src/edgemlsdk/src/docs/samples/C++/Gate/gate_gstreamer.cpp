#include <iostream>
#include <string>

#include <Panorama/gst.h>
#include <Panorama/edge/event_broker.h>
#include <Panorama/aws.h>
using namespace Panorama;

int main(int argc, char* argv[])
{
    /*
    Starts a simple pipeline with a gate.  Shows code to control the gate directly with native GStreamer APIs
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

    GstElement* gst_pipeline = pipeline->Element();
    GstElement* gate = gst_bin_get_by_name(GST_BIN(gst_pipeline), "g1");

    // Start the pipeline
    CHECKHR(pipeline->Start());

    int num_frames = 0;
    do
    {
        std::cout << "Enter number of frames to pass through gate (-1 to exit): ";
        std::cin >> num_frames;

        if(num_frames >= 0)
        {
            g_object_set(G_OBJECT(gate), "numframes", num_frames, NULL);
        }
    } while (num_frames >= 0);

    CHECKHR(pipeline->Stop());

    gst_object_unref(gate);

    CHECKHR(GStreamer::Shutdown());
    return 0;
}