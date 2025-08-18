#include <nlohmann/json.hpp>

#include <Panorama/trace.h>
#include <Panorama/app.h>
#include <Panorama/gst.h>

using namespace Panorama;

void CreatePropertiesFile(const std::string& pattern)
{
    // Create a file to hold some variables.
    FILE* fptr = fopen("variables_sample.json", "w");

    nlohmann::json variables;
    
    nlohmann::json string_variable;
    string_variable["type"] = "string";
    string_variable["immutable"] = true;
    string_variable["value"] = pattern;

    variables["pattern"] = string_variable;

    fprintf(fptr, "%s", variables.dump().c_str());
    fclose(fptr);
}

int main()
{
    ADD_CONSOLE_TRACE;
    HRESULT hr = S_OK;

    // Create a file that can be loaded by the FilePropertyDelegate that contain a variable
    CreatePropertiesFile("snow");

    // Create the file property delegate to read from that file
    ComPtr<IPropertyDelegate> file_delegate;
    CHECKHR(CreateFilePropertyDelegate(file_delegate.AddressOf(), "./variables_sample.json"));

    // Create the application object.
    ComPtr<IApp> app = App::Create();
    CHECKNULL(app, E_FAIL);

    // Add the property delegate to the application object
    CHECKHR(app->AddPropertyDelegate(file_delegate));

    // Initialize the GStreamer framework
    CHECKHR(GStreamer::Initialize());

    // Create the pipeline object with a simple definition
    ComPtr<IPipeline> pipeline;
    CHECKHR(Pipeline::Create(pipeline.AddressOf(), "my-pipeline", "videotestsrc name=src pattern=${pattern} ! ximagesink", app));

    // Start the pipeline
    CHECKHR(pipeline->Start());

    // Wait for a few seconds then update the file with a new pattern
    ThreadSleep(3000);
    CreatePropertiesFile("ball");

    // Synchronize the application to update changes to properties
    ComPtr<IPropertyCollection> changed_properties; // not used in this example
    CHECKHR(app->Synchronize(changed_properties.AddressOf()));

    // Refresh the pipeline to apply property changes.  Since the value of pattern is marked as immutable it will cause the pipeline to restart.
    CHECKHR(pipeline->Refresh());

    // Wait for a few seconds then shutdown
    ThreadSleep(3000);
    CHECKHR(pipeline->Stop()); // Happens automatically when pipeline goes out of scope, here for completeness

    CHECKHR(GStreamer::Shutdown());
    return hr;
}