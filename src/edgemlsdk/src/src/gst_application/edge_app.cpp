#include <thread>
#include <vector>

#include <nlohmann/json.hpp>

#include <Panorama/gst.h>
#include <Panorama/gst_application.h>
#include <Panorama/eventing.h>
#include <env_vars.h>
#include <misc.h>

#include "edge_app.h"

using namespace Panorama;

class EdgeApp : public UnknownImpl<IEdgeApp>
{
public:
    static HRESULT Create(IEdgeApp** ppObj, IEdgeAppConfig* config, PluginLoader plugin_loader)
    {
        HRESULT hr = S_OK;
        CREATE_COM(EdgeApp, ptr);
        CHECKHR_MSG(ptr->Initialize(config, plugin_loader), "Failed to initialize the EdgeApp");
        *ppObj = ptr.Detach();
        return hr;
    }

    ~EdgeApp()
    {
        COM_DTOR(EdgeApp);
        _message_broker->Unsubscribe(_synchronize_sub_token);

        Stop();
        _shutdownEvt.Set();

        if(_heartbeat_thread.joinable())
        {
            _heartbeat_thread.join();
        }

        COM_DTOR_FIN(EdgeApp);
    }

    HRESULT Start() override
    {
        HRESULT hr = S_OK; 
        CHECKNULL_MSG(_pipeline_manager, E_INVALID_STATE, "Trying to start edge app but the internal pipeline manager is null");

        // Possible the pipelines property hasn't been specified.  Shouldn't stop the application from starting
        // as this property might be defined later
        ComPtr<IStringProperty> pipelines_property;
        hr = _app->GetStringProperty(pipelines_property.AddressOf(), "pipelines");
        if(FAILED(hr))
        {
            TraceVerbose("Failed to get pipelines property, delaying pipeline manager initialize");
            return S_FALSE;
        }

        CHECKHR(_pipeline_manager->Initialize());
        CHECKHR(_pipeline_manager->Start());
        return hr;
    }

    HRESULT Stop() override
    {
        HRESULT hr = S_OK;

        if(_pipeline_manager != nullptr)
        {
            CHECKHR(_pipeline_manager->Stop());
        }

        return hr;
    }

    HRESULT GetMessageBroker(IMessageBroker** ppObj) override
    {
        HRESULT hr = S_OK;
        CHECKNULL(ppObj, E_POINTER);
        _message_broker.AddRef();
        *ppObj = _message_broker;
        return hr;
    }

private:
    HRESULT Initialize(IEdgeAppConfig* config, PluginLoader plugin_loader)
    {
        HRESULT hr = S_OK;
        CHECKNULL_MSG(config, E_INVALIDARG, "EdgeAppConfig cannot be null");
        _config = config;

        CHECKHR(_config->AppContext(_app.AddressOf()));

        // Initialize event broker before GSTreamer initialization so tracers are wired up correctly
        CHECKHR_MSG(InitializeMessageBroker(_app), "Failed to initialize event broker");

        CHECKHR(_message_broker->Subscribe("Syncrhonize", [&](IPayload* payload)
        {
            this->Synchronize();
        }));
        _synchronize_sub_token = hr;

        // Initialize GStreamer with the appropriate log level
        CHECKHR_MSG(GStreamer::Initialize(_config->GstLogLevel()), "Failed to initialize GStreamer");

        // Create and start the pipeline manager
        CHECKHR_MSG(CreatePipelineManager(_pipeline_manager.AddressOf(), _app), "Failed to create pipeline manager");

        // Subscribe to various pipeline manager events
        CHECKNULL(_pipeline_manager->OnPipelineAdded([&](IPipeline* pipeline)
        {
            this->OnPipelineAdded(pipeline);
        }), E_FAIL);

        // Load the application plugins
        if(plugin_loader != nullptr)
        {
            CHECKHR_MSG(LoadPlugins(plugin_loader), "Failed loading an application plugin, see logs for details");
        }

        CHECKHR(_message_broker->Initialize());

        // Start thread that will publish heartbeat messages
        // Thread will end when the shutdownEvt has been signaled.
        _heartbeat_thread = std::thread([&]()
        {
            HeartbeatThread();
        });

        return hr;
    }

    HRESULT InitializeMessageBroker(IApp* app)
    {
        HRESULT hr = S_OK;
        ComPtr<IStringProperty> message_broker_config;
        hr = app->GetStringProperty(message_broker_config.AddressOf(), "message_broker_config");
        if(SUCCEEDED(hr))
        {
            MessageBroker::SetDefaultConfig(message_broker_config->Get());
        }

        CHECKHR(MessageBroker::Create(_message_broker.AddressOf(), app));
        return hr;
    }

    HRESULT LoadPlugins(PluginLoader plugin_loader)
    {
        HRESULT hr = S_OK;

        ComPtr<IStringProperty> app_plugins_property;
        hr = _app->GetStringProperty(app_plugins_property.AddressOf(), "app_plugins");
        if(FAILED(hr) && hr != E_NOT_FOUND)
        {
            TraceError("Error retrieving `app_plugins` property: %s", ErrorCodeToString(hr));
            return hr;
        }

        if(hr == E_NOT_FOUND)
        {
            return S_FALSE;
        }

        if(nlohmann::json::accept(app_plugins_property->Get()) == false)
        {
            TraceError("app_plugins property is not valid json");
            return E_INVALIDARG;
        }

        nlohmann::json jObj = nlohmann::json::parse(app_plugins_property->Get());
        for (nlohmann::json::iterator iter = jObj.begin(); iter != jObj.end(); iter++)
        {
            CHECKHR(ValidateJsonProperty<const char*>(iter.value(), "type", true));
            CHECKHR(ValidateJsonProperty<const char*>(iter.value(), "location", true));

            std::string type_str = iter.value()["type"];

            PluginDescriptor descriptor;
            descriptor.Location = iter.value()["location"];
            if(type_str.compare("python") == 0)
            {
                descriptor.Type = PluginType::Python;
            }
            else if(type_str.compare("cpp") == 0)
            {
                descriptor.Type = PluginType::Cpp;
            }
            else
            {
                TraceError("Type %s is not supported.  Current accepted values are `python` and `cpp`", iter.value()["type"].dump().c_str());
                return E_INVALIDARG;
            }

            ComPtr<IAppPlugin> plugin;
            CHECKHR(plugin_loader(plugin.AddressOf(), descriptor));
            CHECKHR(plugin->Initialize(_app, _message_broker));
            _app_plugins.push_back(plugin);
            TraceInfo("Loaded plugin %s", plugin->Id());
        }

        return hr;
    }

    HRESULT Synchronize()
    {
        HRESULT hr = S_OK;
        TraceInfo("Synchronizing property delegates");

        // if we need to inform plugins about property changes then pass in a IPropertyCollection
        // and forward onto plugins
        ComPtr<IPropertyCollection> changed_properties;
        CHECKHR(_app->Synchronize(changed_properties.AddressOf()));

        // Refresh the pipeline manager
        CHECKHR(_pipeline_manager->Refresh());

        return hr;
    }

    void HeartbeatThread()
    {
        HRESULT hr;
        nlohmann::json heartbeat_json;
        Timestamp start = NowAsTimestamp();
        while(_shutdownEvt.WaitFor(_config->HeartbeatInterval()) == false)
        {
            Timestamp now = NowAsTimestamp();
            float seconds_alive = static_cast<float>(TimestampToMilliseconds(now - start)) * 0.001f;
            heartbeat_json["duration"] = seconds_alive;

            CHECK_FAIL(_message_broker->PublishAsync("application_health", heartbeat_json.dump().c_str()), );
        }
    }

    void OnPipelineAdded(IPipeline* pipeline)
    {
        // Subscribe to the pipeline error event
        // Publish a monitor event when they occur
        pipeline->OnError([&](IPipeline* sender, IPipelineError* error)
        {
            HRESULT hr;

            if(error != nullptr && sender != nullptr)
            {
                // MonitorEvent evt;
                std::string err_string = error->ToString();
                if(nlohmann::json::accept(err_string) == false)
                {
                    TraceWarning("IPipelineError object did not provide error in valid json");
                    return;
                }

                nlohmann::json error_json;
                error_json["type"] = "error";
                error_json["pipeline_id"] = sender->Id();
                error_json["error"] = nlohmann::json::parse(err_string);

                CHECK_FAIL(_message_broker->PublishAsync("gstreamer", error_json.dump().c_str()), );
            }
        });

        // Subscribe to the pipeline state change events
        pipeline->OnStateChange([&](IPipeline* sender, int32_t old_state, int32_t new_state)
        {
            if(sender != nullptr)
            {
                HRESULT hr;

                nlohmann::json state_change_json;
                state_change_json["type"] = "state_change";
                state_change_json["pipeline_id"] = sender->Id();
                state_change_json["old_state"] = old_state;
                state_change_json["new_state"] = new_state;

                CHECK_FAIL(_message_broker->PublishAsync("gstreamer", state_change_json.dump().c_str()), );
            }
        });
    }

    ComPtr<IPipelineManager> _pipeline_manager;
    ComPtr<IEdgeAppConfig> _config;
    ComPtr<IApp> _app;
    ComPtr<IMessageBroker> _message_broker;
    std::thread _heartbeat_thread;
    ManualResetEvent _shutdownEvt;
    std::vector<ComPtr<IAppPlugin>> _app_plugins;
    int32_t _synchronize_sub_token = 0;
};

DLLAPI HRESULT CreateEdgeApp(IEdgeApp** ppObj, IEdgeAppConfig* config, PluginLoader plugin_loader)
{
    return EdgeApp::Create(ppObj, config, plugin_loader);
}