#include <string>
#include <map>
#include <list>

#include <gst/gst.h>
#include <nlohmann/json.hpp>

#include <Panorama/flowcontrol.h>
#include <Panorama/comptr.h>
#include <Panorama/gst.h>
#include <misc.h>


using namespace Panorama;

class PipelineManagerImpl : public UnknownImpl<IPipelineManager>
{
public:
    static HRESULT Create(IPipelineManager** ppObj, IApp* app)
    {
        HRESULT hr = S_OK;
        CREATE_COM(PipelineManagerImpl, ptr);
        CHECKHR(ptr->_Initialize(app));

        *ppObj = ptr.Detach();
        return hr;
    }

    ~PipelineManagerImpl()
    {
        COM_DTOR(PipelineManagerImpl);

        // Stop all the pipelines
        TraceDebug("Shutting down PipelineManager");
        for (auto it = _pipelines.begin(); it != _pipelines.end(); ++it) 
        {
            it->second->Stop();
        }

        _pipelines.clear();
        COM_DTOR_FIN(PipelineManagerImpl);
    }

    HRESULT Start() override
    {
        for (auto it = _pipelines.begin(); it != _pipelines.end(); ++it) 
        {
            HRESULT hr = it->second->Start();
            if(FAILED(hr))
            {
                TraceWarning("Unable to start pipeline %s", it->second->Id());
                continue;
            }
        }

        _startPipelinesOnAdd = true;
        return S_OK;
    }

    HRESULT Restart() override
    {
        for (auto it = _pipelines.begin(); it != _pipelines.end(); ++it) 
        {
            HRESULT hr = it->second->Restart();
            if(FAILED(hr))
            {
                TraceWarning("Unable to restart pipeline %s", it->second->Id());
                continue;
            }
        }

        return S_OK;
    }

    HRESULT Stop() override
    {
        for (auto it = _pipelines.begin(); it != _pipelines.end(); ++it) 
        {
            HRESULT hr = it->second->Stop();
            if(FAILED(hr))
            {
                TraceWarning("Unable to stop pipeline %s", it->second->Id());
                continue;
            }
        }

        return S_OK;
    }

    int32_t Count() override
    {
        return _pipelines.size();
    }

    HRESULT GetPipelineById(IPipeline** ppObj, const char* Id) override
    {
        HRESULT hr = S_OK;
        CHECKNULL(ppObj, E_POINTER);
        CHECKNULL(Id, E_INVALIDARG);
        CHECKIF(_pipelines.find(Id) == _pipelines.end(), E_INVALIDARG);
        CHECKIF_MSG(_pipelines.find(Id) == _pipelines.end(), E_NOT_FOUND, "Pipeline ID cannot be found");

        _pipelines[Id].AddRef();
        *ppObj = _pipelines[Id];
        return hr;
    }
    
    HRESULT AddPipeline(IPipelineDefinition* pipeline_def) override
    {
        HRESULT hr = S_OK;
        std::string id = pipeline_def->Id();
        std::string definition = pipeline_def->GetDefinition();
        bool retry_enabled = _pipelines_retry_enabled[id];

        CHECKIF(id.empty(), E_INVALIDARG);
        CHECKIF(definition.empty(), E_INVALIDARG);

        if(_pipelines.find(id) != _pipelines.end())
        {
            TraceWarning("Pipeline %s already exists in pipeline manager", id.c_str());
            return S_FALSE;
        }

        // Invoke the add pipeline preview callback
        bool userHandled = false;
        auto eventHandlers(_eventHandler);
        for(const auto& elem : eventHandlers)
        {
            userHandled = userHandled || elem->OnPipelineAddPreview(pipeline_def);
        }

        if(userHandled)
        {
            // User has indicated they will handle the pipeline add
            TraceVerbose("PipelineManager: User will handle adding of pipeline %s", pipeline_def->Id());
            return hr;
        }

        ComPtr<IPipeline> pipeline;
        CHECKHR(CreatePipeline(pipeline.AddressOf(), pipeline_def->Id(), pipeline_def->GetDefinition(), _app));
        for(const auto& elem : eventHandlers)
        {
            elem->OnPipelineAdded(pipeline);
        }

        if(_retryMechanism != nullptr && retry_enabled)
        {
            pipeline->AddPipelineEventHandler(_retryMechanism);
        }

        // Possible StartAll has not been called yet.  
        // Don't start the pipeline just yet
        if(_startPipelinesOnAdd)
        {
            CHECKHR(pipeline->Start());
        }

        _pipelines[id] = pipeline;
        TraceInfo("New Pipeline is Added: id = %s, launch string = %s, retry_enabled = %s", id.c_str(), definition.c_str(), retry_enabled ? "true" : "false");

        return hr;
    }

    HRESULT RemovePipeline(const char* pipelineId) override
    {
        HRESULT hr = S_OK;
        CHECKNULL(pipelineId, E_INVALIDARG);

        if(_pipelines.find(pipelineId) == _pipelines.end())
        {
            TraceWarning("PipelineManager: Removing pipeline %s, but not found", pipelineId);
            return S_FALSE;
        }

        ComPtr<IPipeline> pipeline = _pipelines[pipelineId];

        // Invoke the pipeline removed preview callback
        bool userHandled = false;
        auto eventHandlers(_eventHandler);
        for(const auto& elem : eventHandlers)
        {
            userHandled = userHandled || elem->OnPipelineRemovePreview(pipeline);
        }

        if(userHandled)
        {
            // User has indicated they will handle the pipeline add
            TraceVerbose("PipelineManager: User will handle removing of pipeline %s", pipelineId);
            return hr;
        }
        if(_retryMechanism != nullptr && _pipelines_retry_enabled[pipelineId])
        {
            _pipelines[pipelineId]->RemovePipelineEventHandler(_retryMechanism);
        }

        pipeline->Stop();
        _pipelines.erase(pipelineId);
        TraceInfo("PipelineManager: Pipeline %s has been removed", pipelineId);

        for(const auto& elem : eventHandlers)
        {
            elem->OnPipelineRemoved(pipeline);
        }

        return hr;
    }

    HRESULT UpdatePipeline(IPipelineDefinition* pipeline_def) override
    {
        HRESULT hr;

        std::string id = pipeline_def->Id();
        std::string new_definition = pipeline_def->GetDefinition();

        CHECKIF(id.empty(), E_INVALIDARG);
        CHECKIF(new_definition.empty(), E_INVALIDARG);

        if(_pipelines.find(id) == _pipelines.end())
        {
            return AddPipeline(pipeline_def);
        }

        std::string old_definition = _pipelines[id]->Definition();
        TraceInfo("Pipeline [%s] is changed from '%s' to '%s'", id.c_str(), old_definition.c_str(), new_definition.c_str());
        CHECKHR(_pipelines[id]->ChangeDefinition(new_definition.c_str()));
        
        return hr;
    }

    HRESULT AddPipelineManagerEventHandler(IPipelineManagerEventHandler* handler) override
    {
        TraceVerbose("Adding event handler at %p to pipeline manager", handler);
        CHECKNULL(handler, E_INVALIDARG);
        _eventHandler.push_back(handler);

        return S_OK;
    }

    void RemovePipelineManagerEventHandler(IPipelineManagerEventHandler* handler) override
    {
        if(handler == nullptr)
        {
            return;
        }

        TraceVerbose("Removing event handler at %p from pipeline manager", handler);
        auto it = std::find(_eventHandler.begin(), _eventHandler.end(), handler);
        if(it != _eventHandler.end())
        {
            _eventHandler.erase(it);
        }
    }

    HRESULT SetRetryMechanism(IPipelineEventHandler* retry_mechanism) override{
        HRESULT hr = S_OK;
        CHECKNULL(retry_mechanism, E_INVALIDARG);
        if (_retryMechanism!=nullptr)
        {
            for (auto it = _pipelines.begin(); it != _pipelines.end(); ++it) 
            {
                if(_pipelines_retry_enabled[it->first])
                {
                    it->second->RemovePipelineEventHandler(_retryMechanism);
                }
            }
        }    
        _retryMechanism = retry_mechanism;
        return hr;
    }

    HRESULT GetRetryMechanism(IPipelineEventHandler** ppObj) override
    {
        CHECKNULL(ppObj, E_POINTER);
        CHECKNULL(_retryMechanism, E_POINTER);
        _retryMechanism.AddRef();
        *ppObj = _retryMechanism;
        return S_OK;
    }

    HRESULT Initialize() override
    {
        HRESULT hr = S_OK;

        // Create Retry Mechanism
        ComPtr<IStringProperty> retry_configuration;
        if(SUCCEEDED(_app->GetStringProperty(retry_configuration.AddressOf(), "retry")))
        {
            CHECKHR(CreateRetryMechanismFromProperty(_retryMechanism.AddressOf(), retry_configuration));
        }
        else
        {
            CHECKHR(CreateRetryMechanismDefault(_retryMechanism.AddressOf()));
        }

        // Create each pipeline
        CHECKHR(HandlePipelines(nullptr));
        return hr;
    }

    HRESULT Refresh() override
    {
        HRESULT hr = S_OK;
        TraceInfo("PipelineManager: Refreshing");
        std::list<ComPtr<IPipeline>> pipelines_to_refresh;

        CHECKHR(HandlePipelines(&pipelines_to_refresh));
        for(auto iter = pipelines_to_refresh.begin(); iter != pipelines_to_refresh.end(); iter++)
        {
            CHECKHR(iter->Ptr()->Refresh());
        }

        return hr;
    }

private:
    HRESULT _Initialize(IApp* app)
    {
        HRESULT hr = S_OK;
        CHECKNULL(app, E_INVALIDARG);
        _app = app;
        return hr;
    }

    HRESULT HandlePipelines(std::list<ComPtr<IPipeline>>* non_changed_pipelines)
    {
        HRESULT hr = S_OK;

        // Get the pipelines property
        ComPtr<IStringProperty> pipelines_property;
        CHECKHR_MSG(_app->GetStringProperty(pipelines_property.AddressOf(), "pipelines"), "pipelines parameter was not defined or is not a string");

        // Ensure pipelines property is JSON
        std::string pipelinesStr = pipelines_property->Get();
        CHECKIF_MSG(nlohmann::json::accept(pipelinesStr) == false, E_INVALIDARG, "pipelines parameters is not valid JSON");
        nlohmann::json jobj = nlohmann::json::parse(pipelinesStr);

        // map to see if an id is still present in the pipelines property
        std::map<std::string, bool> present;
        for (const auto& kv_pair : _pipelines)
        {
            present[kv_pair.first] = false;
        }

        // Loop through pipelines
        for(const auto& elem : jobj)
        {
            std::string foo = elem.dump();

            CHECKIF_MSG(ValidateJsonProperty<const char*>(elem, "id", true) == false, E_INVALIDARG, "PipelineManager: id was not defined or is not a string");
            CHECKIF_MSG(ValidateJsonProperty<const char*>(elem, "definition", true) == false, E_INVALIDARG, "PipelineManager: definition was not defined or is not a string");
            CHECKIF_MSG(ValidateJsonProperty<bool>(elem, "retry_enabled", false) == false, E_INVALIDARG, "PipelineManager: retry_enabled is not a boolean");

            bool retry_enabled = false;
            if(elem.contains("retry_enabled"))
            {
                retry_enabled = elem["retry_enabled"];
            }

            std::string id = elem["id"];
            std::string def = elem["definition"];
            _pipelines_retry_enabled[id] = retry_enabled;

            if(_pipelines.find(elem["id"]) == _pipelines.end())
            {
                // Pipeline is currently not in list, create a new pipeline
                ComPtr<IPipelineDefinition> pipelineDef;
                CHECKHR(CreatePipelineDefinition(pipelineDef.AddressOf(), id.c_str(), def.c_str()));
                CHECKHR(AddPipeline(pipelineDef));
                // HRESULT addHr = AddPipeline(pipelineDef);
                // if(FAILED(addHr))
                // {
                //     TraceWarning("PipelineManager: Could not add pipeline %s, see logs for details", pipelineDef->Id());
                //     continue;
                // }
            }
            else
            {
                // Pipeline is present in the current list
                present[id] = true;
                if(strcmp(_pipelines[id]->Definition(), def.c_str()))
                {
                    // Pipelines has changed definition, kick off a restart
                    // Invoke the definition changed preview
                    bool userHandled = false;
                    auto eventHandlers(_eventHandler);
                    for(const auto& elem : eventHandlers)
                    {
                        userHandled = userHandled || elem->OnDefintionChangePreview(_pipelines[id], def.c_str());
                    }

                    if(userHandled)
                    {
                        // User has indicated they will handle the pipeline add
                        TraceVerbose("PipelineManager: User will handle updating pipeline %s", id.c_str());
                    }
                    else
                    {
                        TraceInfo("Pipeline %s has a different definition, restarting", id.c_str());
                        CHECKHR(_pipelines[id]->ChangeDefinition(def.c_str()));
                    }
                }
                else
                {
                    // List of pipelines that have not changed.
                    // List is populated because this method is also called from Refresh and 
                    // need to call Refresh on pipelines that are currently managed but didn't have their definition changed
                    // as their properties may have changed
                    if(non_changed_pipelines != nullptr)
                    {
                        non_changed_pipelines->push_back(_pipelines[id]);
                    }
                }
            }
        }

        // Loop through the present std::map to determine which pipelines are no longer in the pipelines property
        // and should be removed
        for(const auto& kv_pair : present)
        {
            if(kv_pair.second == false)
            {
                HRESULT removeHr = RemovePipeline(kv_pair.first.c_str());
                if(FAILED(removeHr))
                {
                    TraceWarning("Could not remove pipeline %s, see logs for details", kv_pair.first.c_str());
                    continue;
                }
            }
        }

        return hr;
    }

    std::map<std::string, ComPtr<IPipeline>> _pipelines;
    std::map<std::string, bool> _pipelines_retry_enabled;
    int64_t _default_handler = 0;
    ComPtr<IPropertyDelegate> _propertyDelegate;
    ComPtr<IStringProperty> _pipelinesProperty;
    ComPtr<IPipelineEventHandler> _retryMechanism;

    std::list<ComPtr<IPipelineManagerEventHandler>> _eventHandler;
    int64_t _cancellationToken = 0;
    ComPtr<IApp> _app;

    // Flag to indicate if a pipeline should be started when it's added.
    // Possible to add pipelines before StartAll is called.  Default is 
    // false and is set to true when StartAll is called.
    bool _startPipelinesOnAdd = false;
};

DLLAPI HRESULT CreatePipelineManager(IPipelineManager** ppObj, IApp* app)
{
    return PipelineManagerImpl::Create(ppObj, app);
}