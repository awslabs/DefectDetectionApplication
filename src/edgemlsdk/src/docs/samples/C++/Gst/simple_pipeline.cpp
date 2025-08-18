#include <Panorama/trace.h>
#include <Panorama/app.h>
#include <Panorama/gst.h>

using namespace Panorama;

int main()
{
    ADD_CONSOLE_TRACE;
    HRESULT hr = S_OK;

    // Create the application object.
    ComPtr<IApp> app = App::Create();

    // Initialize the GStreamer framework
    GStreamer::Initialize();

    // Create the pipeline object with a simple definition
    ComPtr<IPipeline> pipeline;
    CHECKHR(Pipeline::Create(pipeline.AddressOf(), "my-pipeline", "videotestsrc ! ximagesink", app));

    // Start the pipeline
    pipeline->Start();

    // Wait for a few seconds then stop the pipeline
    ThreadSleep(3000);
    pipeline->Stop(); // Happens automatically when pipeline goes out of scope, here for completeness

    GStreamer::Shutdown();
    return hr;
}