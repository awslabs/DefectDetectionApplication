#include <nlohmann/json.hpp>

#include <Panorama/trace.h>
#include <Panorama/app.h>
#include <Panorama/gst.h>

using namespace Panorama;

void CreatePropertiesFile1()
{
    // Create a file to hold some variables.
    FILE* fptr = fopen("variables_sample.json", "w");

    nlohmann::json doc;
    
    nlohmann::json pattern;
    pattern["type"] = "string";
    pattern["immutable"] = true;
    pattern["value"] = "snow";

    nlohmann::json pipeline1;
    pipeline1["id"] = "pipeline1";
    pipeline1["definition"] = "videotestsrc name=src pattern=${pattern} ! ximagesink";

    nlohmann::json pipeline2;
    pipeline2["id"] = "pipeline2";
    pipeline2["definition"] = "videotestsrc ! ximagesink";

    nlohmann::json pipelines;
    pipelines.push_back(pipeline1);
    pipelines.push_back(pipeline2);

    doc["pattern"] = pattern;
    doc["pipelines"] = pipelines;

    fprintf(fptr, "%s", doc.dump().c_str());
    fclose(fptr);
}

void CreatePropertiesFile2()
{
    // Create a file to hold some variables.
    FILE* fptr = fopen("variables_sample.json", "w");

    nlohmann::json doc;
    
    nlohmann::json pattern;
    pattern["type"] = "string";
    pattern["immutable"] = true;
    pattern["value"] = "ball";

    nlohmann::json pipeline1;
    pipeline1["id"] = "pipeline1";
    pipeline1["definition"] = "videotestsrc name=src pattern=${pattern} ! ximagesink";

    nlohmann::json pipelines;
    pipelines.push_back(pipeline1);

    doc["pattern"] = pattern;
    doc["pipelines"] = pipelines;

    fprintf(fptr, "%s", doc.dump().c_str());
    fclose(fptr);
}


int main()
{
    ADD_CONSOLE_TRACE;
    HRESULT hr = S_OK;

    // Create a file that can be loaded by the FilePropertyDelegate that contain a variable
    CreatePropertiesFile1();

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

    // Create the pipeline manager
    ComPtr<IPipelineManager> mgr;
    CHECKHR(PipelineManager::Create(mgr.AddressOf(), app));
    CHECKHR(mgr->Initialize());

    // Start the manager, which will start all the pipelines
    CHECKHR(mgr->Start());

    // Wait for a few seconds then update the properties file
    ThreadSleep(3000);
    CreatePropertiesFile2();

    // Synchronize the application to update changes to properties
    ComPtr<IPropertyCollection> changed_properties; // not used in this example
    CHECKHR(app->Synchronize(changed_properties.AddressOf()));

    // Refresh the manager to apply property changes.  Since the value of pattern is marked as immutable it will cause the pipeline1 to restart.  pipeline2 was removed, so it will be stopped and removed.
    CHECKHR(mgr->Refresh());

    // Wait for a few seconds then shutdown
    ThreadSleep(3000);
    CHECKHR(mgr->Stop()); // Happens automatically when pipeline manager goes out of scope, here for completeness

    CHECKHR(GStreamer::Shutdown());
    return hr;
}