
#include <thread>
#include <fstream>

#include <gtest/gtest.h>
#include <nlohmann/json.hpp>

#include <Panorama/app.h>
#include <Panorama/gst.h>
#include <misc.h>
#include <env_vars.h>
#include <gst_application/edge_app.h>
#include <TestUtils.h>

using namespace std;
using namespace Panorama;

HRESULT CreateAppWithCliArgs(IApp** ppObj, const std::string& cli_args)
{
    HRESULT hr = S_OK;
    int argc;
    char** argv;

    std::string cli = "exeName " + cli_args;
    CommandLineArgs args = CreateCommandLineArgs(cli.c_str());
    hr = CreatePanoramaApp(ppObj, args.Count(), args.Values());
    return hr;
}

TEST(EdgeApp, Config)
{
    HRESULT hr = S_OK;

    {
        // Empty configuration
        ComPtr<IApp> app;
        ASSERT_S(CreateAppWithCliArgs(app.AddressOf(), ""));
        
        ComPtr<IEdgeAppConfig> config;
        ASSERT_S(CreateEdgeAppConfig_v1(config.AddressOf(), app));
        ASSERT_TRUE(config->GstLogLevel() == TraceLevelToGstLevel(Tracer::GetTraceLevel()));
    }

    {
        // Non JSON environment
        ComPtr<IApp> app;
        ASSERT_S(CreateAppWithCliArgs(app.AddressOf(), "--environment \"GST_DEBUG\":\"10\""));
        
        ComPtr<IEdgeAppConfig> config;
        ASSERT_F(CreateEdgeAppConfig_v1(config.AddressOf(), app));
    }

    {
        // Setting GST_DEBUG level
        ComPtr<IApp> app;
        ASSERT_S(CreateAppWithCliArgs(app.AddressOf(), "--environment {\"GST_DEBUG\":\"1\"}"));
        
        ComPtr<IEdgeAppConfig> config;
        ASSERT_S(CreateEdgeAppConfig_v1(config.AddressOf(), app));
        ASSERT_TRUE(config->GstLogLevel() == 1);
        ASSERT_FALSE(strcmp(GetEnvVar("GST_DEBUG").c_str(), "1"));
        UnsetEnvVar("GST_DEBUG");
    }

    {
        // Setting GST_DEBUG level out of range
        ComPtr<IApp> app;
        ASSERT_S(CreateAppWithCliArgs(app.AddressOf(), "--environment {\"GST_DEBUG\":\"10\"}"));
        
        ComPtr<IEdgeAppConfig> config;
        ASSERT_F(CreateEdgeAppConfig_v1(config.AddressOf(), app));
        UnsetEnvVar("GST_DEBUG");
    }
}

TEST(EdgeApp, Create)
{
    HRESULT hr = S_OK;

    {
        // Creating the edge app with the pipelines explicitly defined
        ComPtr<IApp> app;
        ASSERT_S(CreateAppWithCliArgs(app.AddressOf(), "--pipelines [{\"id\": \"foo\", \"definition\":\"videotestsrc ! fakesink\"}]"));
        
        ComPtr<IEdgeAppConfig> config;
        ASSERT_S(CreateEdgeAppConfig_v1(config.AddressOf(), app));
        
        ComPtr<IEdgeApp> edge_app;
        ASSERT_S(CreateEdgeApp(edge_app.AddressOf(), config));
    }
    {
        // Creating the edge app without providing the pipelines definition
        ComPtr<IApp> app;
        ASSERT_S(CreateAppWithCliArgs(app.AddressOf(), ""));
        
        ComPtr<IEdgeAppConfig> config;
        ASSERT_S(CreateEdgeAppConfig_v1(config.AddressOf(), app));
        
        ComPtr<IEdgeApp> edge_app;
        ASSERT_S(CreateEdgeApp(edge_app.AddressOf(), config));
        ASSERT_S(edge_app->Start());
    }
}

TEST(EdgeApp, Heartbeats)
{
    HRESULT hr = S_OK;

    // Creating the edge app with the pipelines explicitly defined
    ComPtr<IApp> app;
    ASSERT_S(CreateAppWithCliArgs(app.AddressOf(), "--heartbeat_interval 1"));
    
    ComPtr<IEdgeAppConfig> config;
    ASSERT_S(CreateEdgeAppConfig_v1(config.AddressOf(), app));
    
    ComPtr<IEdgeApp> edge_app;
    ASSERT_S(CreateEdgeApp(edge_app.AddressOf(), config));

    ComPtr<IMessageBroker> broker;
    ASSERT_S(edge_app->GetMessageBroker(broker.AddressOf()));

    AutoResetEvent heartbeat_published;
    ASSERT_S(broker->Subscribe("application_health", [&](IPayload* payload)
    {
        ASSERT_TRUE(nlohmann::json::accept(payload->SerializeAsString()));
        nlohmann::json json = nlohmann::json::parse(payload->SerializeAsString());
        ASSERT_TRUE(ValidateJsonProperty<float>(json, "duration"));
        heartbeat_published.Set();
    }));

    ASSERT_TRUE(heartbeat_published.WaitFor(10));
}

TEST(EdgeApp, PipelineErrorTests)
{
    HRESULT hr = S_OK;

    // Creating the edge app with the pipelines explicitly defined
    ComPtr<IApp> app;
    ASSERT_S(CreateAppWithCliArgs(app.AddressOf(), "--pipelines [{\"id\": \"foo\", \"definition\":\"videotestsrc num-buffers=5 ! fakesink\"}]"));

    ComPtr<IEdgeAppConfig> config;
    ASSERT_S(CreateEdgeAppConfig_v1(config.AddressOf(), app));
    
    ComPtr<IEdgeApp> edge_app;
    ASSERT_S(CreateEdgeApp(edge_app.AddressOf(), config));

    ComPtr<IMessageBroker> broker;
    ASSERT_S(edge_app->GetMessageBroker(broker.AddressOf()));

    ManualResetEvent pipeline_error_published;
    ASSERT_S(broker->Subscribe("gstreamer", [&](IPayload* payload)
    {
        ASSERT_TRUE(nlohmann::json::accept(payload->SerializeAsString()));
        nlohmann::json json = nlohmann::json::parse(payload->SerializeAsString());
        ASSERT_TRUE(ValidateJsonProperty<const char*>(json, "type"));

        std::string type = json["type"];
        if(type.compare("error") == 0)
        {
            ASSERT_TRUE(ValidateJsonProperty<nlohmann::json>(json, "error"));
            ASSERT_TRUE(ValidateJsonProperty<const char*>(json, "pipeline_id"));
            pipeline_error_published.Set();
        }
    }));

    ASSERT_S(edge_app->Start());
    ASSERT_TRUE(pipeline_error_published.WaitFor(10));
}

TEST(EdgeApp, PipelineStateChange)
{
    HRESULT hr = S_OK;

    // Creating the edge app with the pipelines explicitly defined
    ComPtr<IApp> app;
    ASSERT_S(CreateAppWithCliArgs(app.AddressOf(), "--pipelines [{\"id\": \"foo\", \"definition\":\"videotestsrc ! fakesink\"}]"));
    
    ComPtr<IEdgeAppConfig> config;
    ASSERT_S(CreateEdgeAppConfig_v1(config.AddressOf(), app));
    
    ComPtr<IEdgeApp> edge_app;
    ASSERT_S(CreateEdgeApp(edge_app.AddressOf(), config));

    ComPtr<IMessageBroker> broker;
    ASSERT_S(edge_app->GetMessageBroker(broker.AddressOf()));

    ManualResetEvent state_change_published;
    broker->Subscribe("gstreamer", [&](IPayload* payload)
    {
        ASSERT_TRUE(nlohmann::json::accept(payload->SerializeAsString()));
        nlohmann::json json = nlohmann::json::parse(payload->SerializeAsString());
        ASSERT_TRUE(ValidateJsonProperty<const char*>(json, "type"));

        std::string type = json["type"];
        if(type.compare("state_change") == 0)
        {
            ASSERT_TRUE(ValidateJsonProperty<const char*>(json, "pipeline_id"));
            ASSERT_TRUE(ValidateJsonProperty<int32_t>(json, "new_state"));
            ASSERT_TRUE(ValidateJsonProperty<int32_t>(json, "old_state"));
            state_change_published.Set();
        }
    });

    ASSERT_S(edge_app->Start());
    ASSERT_TRUE(state_change_published.WaitFor(10));
}