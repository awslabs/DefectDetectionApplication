#include <sstream>
#include <vector>
#include <string>
#include <map>
#include <iostream>
#include <csignal>
#include <unistd.h>
#include <thread>

#include <gst/gst.h>
#include <nlohmann/json.hpp>

#include <Panorama/flowcontrol.h>
#include <Panorama/comptr.h>
#include <Panorama/eventing.h>
#include <Panorama/gst.h>
#include <Panorama/app.h>
#include <Panorama/gst_application.h>
#include <app/device_env.h>
#include <env_vars.h>
#include <misc.h>

#include "edge_app.h"
using namespace Panorama;

#define PIPELINE_DEFINITIONS_KEY "pipelines"

static ManualResetEvent shutdownEvt;

// Define the handler function
void signalHandler(int sig) 
{
    TraceInfo("Received signal %d, shutting down", sig);
    shutdownEvt.Set();
}

HRESULT LoadPlugin(IAppPlugin** ppObj, PluginDescriptor& descriptor)
{
    HRESULT hr = S_OK;
    CHECKNULL(ppObj, E_POINTER);

    ComPtr<IAppPlugin> plugin;
    CHECKHR(LoadAppPlugin(plugin.AddressOf(), descriptor));
    *ppObj = plugin.Detach();

    return hr;
}

HRESULT ParseCliOptions(int argc, char* argv[])
{
    HRESULT hr = S_OK;

    ComPtr<IPropertyDelegate> cli;
    CHECKHR(CreateCLIPropertyDelegate(cli.AddressOf(), argc, argv));

    // Handle trace_level option
    // Using do-while(false) construct to break from withtout using a return
    do
    {
        ComPtr<IProperty> trace_level_property;
        if(FAILED(cli->GetProperty(trace_level_property.AddressOf(), "TraceLevel")))
        {
            break;
        }

        ComPtr<IStringProperty> trace_level = trace_level_property.QueryInterface<IStringProperty>();
        if(trace_level == nullptr)
        {
            break;
        }

        if(strcmp("DEBUG", trace_level->Get()) == 0)
        {
            Tracer::SetTraceLevel(TraceLevel::Debug);
        }
        else if(strcmp("VERBOSE", trace_level->Get()) == 0)
        {
            Tracer::SetTraceLevel(TraceLevel::Verbose);
        }
        else if(strcmp("INFO", trace_level->Get()) == 0)
        {
            Tracer::SetTraceLevel(TraceLevel::Information);
        }
        else if(strcmp("WARNING", trace_level->Get()) == 0)
        {
            Tracer::SetTraceLevel(TraceLevel::Warning);
        }
        else if(strcmp("ERROR", trace_level->Get()) == 0)
        {
            Tracer::SetTraceLevel(TraceLevel::Error);
        }
        else
        {
            TraceWarning("Value %s is not a valid trace_level", trace_level->Get());
        }
    } while(false);

    // Handle http_listener option
    // Using do-while(false) construct to break from withtout using a return
    do
    {
        ComPtr<IProperty> http_property;
        if(FAILED(cli->GetProperty(http_property.AddressOf(), "HttpTraceListener")))
        {
            break;
        }

        ComPtr<IStringProperty> http = http_property.QueryInterface<IStringProperty>();
        if(http == nullptr)
        {
            break;
        }

        std::string ip;
        int32_t port;
        if(FAILED(ParseIpPort(ip, port, http->Get())))
        {
            break;
        }

        ComPtr<ITraceListener> http_trace_listener;
        if(FAILED(Tracer::CreateHttpTraceListener(http_trace_listener.AddressOf(), ip.c_str(), port)))
        {
            TraceWarning("Failed to create http trace listener");
            break;
        }

        // TODO
        // This is unlikely to pass an AppSec review as this is inherently insecure
        // Ultimately we should either wrap this in #DEBUG OR add some authentication
        // Or, who knows, maybe appsec will be OK with it (doubt it though)
        //
        // TODO
        // Known Limitation:  Adding this + setting Verbosity to DEBUG will result in infinte spew of messages
        // Need to refactor our RESTful APIs to not using COM types to avoid this, other solutions may be available.
        // Accepting limitation for now.
        TraceInfo("Adding HttpTraceListener");
        PEEKHR(Tracer::AddTraceListener(http_trace_listener));
    } while(false);

    return S_OK;
}

int main(int argc, char* argv[])
{
    signal(SIGTERM, signalHandler);
    signal(SIGINT, signalHandler);
    HRESULT hr = S_OK;

    try
    {
        ADD_CONSOLE_TRACE;
        CHECKHR(ParseCliOptions(argc, argv));

        // Scope objects to ensure they are destroyed before this method returns
        {
            // Initialize the application with any command line arguments
            ComPtr<IApp> app;
            CHECKHR(CreatePanoramaApp(app.AddressOf(), argc, argv));

            // Get the application configuration
            ComPtr<IEdgeAppConfig> config;
            CHECKHR(CreateEdgeAppConfig_v1(config.AddressOf(), app));

            // Create the runtime context
            ComPtr<IEdgeApp> edge_app;
            CHECKHR(CreateEdgeApp(edge_app.AddressOf(), config, &LoadPlugin));

            // Start the application
            CHECKHR(edge_app->Start());
            shutdownEvt.Wait();
        }
    }
    catch(const std::exception& e)
    {
        TraceError("Uncaught Exception: %s", e.what());
    }

    TraceInfo("Shutdown sequence complete.  Exiting");
    return hr;
}