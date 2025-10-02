#include <string>
#include <iostream>
#include <future>
#include <map>
#include <functional>
#include "triton.h"
#include <nlohmann/json.hpp>
#include <scheduling.h>

#include <Panorama/comobj.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/apidefs.h>
#include <Panorama/comptr.h>
#include <filesystem_safe.h>

using namespace Panorama;
using namespace fs;

class TritonServer : public UnknownImpl<ITritonServer>
{
public:
    HRESULT static Create(ITritonServer** ppObj, const char* modelRepoPath, const char* tritonServerPath)
    {
        COM_FACTORY(TritonServer, Initialize(modelRepoPath, tritonServerPath));
    }

    ~TritonServer()
    {
        COM_DTOR(TritonServer);
        TraceInfo("Shutting down triton server");
        _load_model_job_queue.Stop();
        if(this->_server != nullptr)
        {
            for(auto iter = _modelMetadata.begin(); iter != _modelMetadata.end(); iter++)
            {
                TRITONSERVER_ServerUnloadModel(this->_server, iter.value()["name"].get<std::string>().c_str());
            }

            TRITONSERVER_ServerStop(this->_server);
            TRITONSERVER_ServerDelete(this->_server);
        }

        COM_DTOR_FIN(TritonServer);
    }

     HRESULT _getModelIndex(std::string& buffer) {
        HRESULT hr = S_OK;
        const char* local_buffer;
        TRITONSERVER_Message* repoMetaData;
        size_t sz;
        CHECK_TRITON_RES(TRITONSERVER_ServerModelIndex(this->_server, 0,&repoMetaData));
        CHECK_TRITON_RES(TRITONSERVER_MessageSerializeToJson(repoMetaData, &local_buffer, &sz));
        buffer = local_buffer;
        Cleanup:
            return hr;
    }

    HRESULT _getMetrics(std::string& buffer) {
        HRESULT hr = S_OK;
        const char* local_buffer;
        TRITONSERVER_Metrics* metrics;
        size_t sz;
        CHECK_TRITON_RES(TRITONSERVER_ServerMetrics(this->_server, &metrics));
        CHECK_TRITON_RES(TRITONSERVER_MetricsFormatted(metrics, TRITONSERVER_METRIC_PROMETHEUS, &local_buffer, &sz));
        buffer = local_buffer;
        CHECK_TRITON_RES(TRITONSERVER_MetricsDelete(metrics));
        Cleanup:
            return hr;
    }

    HRESULT _checkIfAdditionalModelsLoaded(const char* ignore_current) {
        HRESULT hr = S_OK;
        nlohmann::json model_index, meta;
        std::string index_buffer;
        TRITONSERVER_Message* modelMetadata;
        const char* buffer;
        size_t sz;
        CHECKHR_MSG(_getModelIndex(index_buffer), "getting Triton Server Index failed");
        CHECKIF_MSG(nlohmann::json::accept(index_buffer) == false, E_FAIL, "JSON returned from Triton Server Index is invalid");
        model_index = nlohmann::json::parse(index_buffer);
        for (const auto& model : model_index) {
            if (model.contains("state") && model.contains("name")) {
                if(model["name"] == ignore_current) {
                    continue;
                }
                if(model["state"] == "READY" && _loaded_models[model["name"]] != true) {
                    CHECK_TRITON_RES(TRITONSERVER_ServerModelMetadata(this->_server, model["name"].get<std::string>().c_str(), -1, &modelMetadata));
                    CHECK_TRITON_RES(TRITONSERVER_MessageSerializeToJson(modelMetadata, &buffer, &sz));
                    CHECKIF_MSG(!nlohmann::json::accept(std::string(buffer)),E_FAIL,"Meta data json from triton is invalid.");
                    meta = nlohmann::json::parse(std::string(buffer));
                    meta["state"] = model["state"];
                    _modelMetadata[model["name"]] = meta;
                    _loaded_models[model["name"]] = true;
                }
            }
        }
        Cleanup:
            return hr;
    }

    HRESULT _checkIfAdditionalModelsUnloaded(const char* ignore_current) {
        HRESULT hr = S_OK;
        nlohmann::json model_index, meta;
        std::string index_buffer;
        TRITONSERVER_Message* modelMetadata;
        const char* buffer;
        size_t sz;
        CHECKHR_MSG(_getModelIndex(index_buffer), "getting Triton Server Index failed");
        CHECKIF_MSG(nlohmann::json::accept(index_buffer) == false, E_FAIL, "JSON returned from Triton Server Index is invalid");
        model_index = nlohmann::json::parse(index_buffer);
        for (const auto& model : model_index) {
            if (model.contains("state") && model.contains("name")) {
                if(model["name"] == ignore_current) {
                    continue;
                }
                if((model["state"] == "UNLOADING" || model["state"] == "UNAVAILABLE") && _loaded_models[model["name"]] == true) {
                    meta["state"] = model["state"];
                    meta["name"] = model["name"];
                    _modelMetadata[model["name"]] = meta;
                    _loaded_models[model["name"]] = false;
                }
            }
        }
        Cleanup:
            return hr;
    }

    HRESULT LoadModel(const char* modelName) override {
        HRESULT hr = S_OK;
        nlohmann::json meta;
        CHECKNULL(modelName, E_INVALIDARG);
        CHECKNULL(this->_server, E_INVALID_STATE);
        std::unique_lock<std::mutex> lock(_metadata_mutex);
        if(_loaded_models.find(modelName) != _loaded_models.end() && _loaded_models[modelName] == true)
        {
            // Model is already loaded
            TraceInfo("Model %s is already loaded", modelName);
            return S_FALSE;
        }
        /*
        * example metadata
        * {
        *   "name": "ensemble",
        *   "state": "READY"
        *   // Some other fields like inputs only available when model is in ready state
        *   "inputs": [
        *     {
        *       "name": "input",
        *       "shape": [1, 3, 224, 224],
        *       "datatype": "FLOAT32"
        *     }
        *   ]
        *   "outputs": [
        *     {
        *       "name": "output",
        *       "shape": [1, 1000],
        *       "datatype": "FLOAT32"
        *     }
        *   ]
        * }
        */
        if(_modelMetadata.find(modelName) == _modelMetadata.end() )
        {
            nlohmann::json m = _modelMetadata[modelName];
            if(m.find("state") != m.end()  && m["state"] == "LOADING")
            {
                // Model is already being loaded
                TraceInfo("Model %s is already being loaded", modelName);
                return S_FALSE;
            }
        }
        TraceInfo("Enqueuing model %s for loading", modelName);
        _load_model_job_queue.Enqueue(std::string(modelName));
        meta["name"] = modelName;
        meta["state"] = "LOADING";
        _modelMetadata[modelName] = meta;
        _loaded_models[modelName] = false;
        return hr;
    }
    HRESULT _LoadModel(const char* modelName)
    {
        HRESULT hr = S_OK;
        nlohmann::json meta, index;
        std::string index_buffer;
        TRITONSERVER_Message* modelMetadata;
        const char* buffer;
        size_t sz;

        TraceInfo("Loading model %s", modelName);
        CHECK_TRITON_RES(TRITONSERVER_ServerLoadModel(_server, modelName));
        CHECK_TRITON_RES(TRITONSERVER_ServerModelMetadata(_server, modelName, -1, &modelMetadata));
        CHECK_TRITON_RES(TRITONSERVER_MessageSerializeToJson(modelMetadata, &buffer, &sz));
        // Update MetaData of loaded model
        CHECKIF_MSG(!nlohmann::json::accept(std::string(buffer)),E_FAIL,"Meta data json from triton is invalid.");
        meta = nlohmann::json::parse(std::string(buffer));
        CHECKHR_MSG(_getModelIndex(index_buffer), "getting Triton Server Index failed");
        CHECKIF_MSG(!nlohmann::json::accept(index_buffer),E_FAIL,"JSON returned from Triton Server Index is invalid");
        index = nlohmann::json::parse(index_buffer);
        meta["state"] = "UNKNOWN";
        for (auto& model : index) {
            if (model.contains("state") && model.contains("name")) {
                if(model["name"] == modelName) {
                    meta["state"] = model["state"];
                    break;
                }
            }
        }
        {
            std::unique_lock<std::mutex> lock(_metadata_mutex);
            _modelMetadata[modelName] = meta;
            _loaded_models[modelName] = true;
            // For ensemble load the internal model metadata as well.
            CHECKHR(_checkIfAdditionalModelsLoaded(modelName));
            TraceInfo("Model %s loaded successfully", modelName);
        }
    Cleanup:
        return hr;
    }

    HRESULT UnloadModel(const char* modelName) override
    {
        HRESULT hr = S_OK;
        nlohmann::json meta;
        CHECKNULL(modelName, E_INVALIDARG);
        CHECKNULL(this->_server, E_INVALID_STATE);
        std::unique_lock<std::mutex> lock(_metadata_mutex);

        if(_loaded_models.find(modelName) != _loaded_models.end() && _loaded_models[modelName] == false)
        {
            // Model is not loaded.
            TraceVerbose("Model %s is not loaded", modelName);
            return S_FALSE;
        }
        TraceInfo("Unloading model %s", modelName);
        /* Unload the requested model. Unloading a model that is not loaded
        on server has no affect and success code will be returned.
        The function does not wait for the requested model to be fully unload
        and success code will be returned.
        Returned error indicates if model unload was unsuccessful.
        */
        CHECK_TRITON_RES(TRITONSERVER_ServerUnloadModelAndDependents(_server, modelName));
        meta["name"] = modelName;
        meta["state"] ="UNLOADING";
        _modelMetadata[modelName] = meta;
        _loaded_models[modelName] = false;
        // Also update unloaded ensemble sub models metadata.
        CHECKHR(_checkIfAdditionalModelsUnloaded(modelName));
    Cleanup:
        return hr;
    }

    const char* ModelMetadata(const char* modelName) override
    {
        HRESULT hr = S_OK;
        std::unique_lock<std::mutex> lock(_metadata_mutex);
        if(!_modelMetadata.contains(modelName))
        {
            TraceVerbose("Could not find metadata for model %s", modelName);
            return nullptr;
        }
        nlohmann::json repo_json;
        std::string index_buffer;
        CHECK_FAIL_MSG(_getModelIndex(index_buffer),nullptr, "getting Triton Server Index failed");
        CHECKIF_MSG(nlohmann::json::accept(index_buffer) == false,nullptr,"JSON returned from Triton Server Index is invalid");
        repo_json = nlohmann::json::parse(index_buffer);
        for (const auto& j : repo_json) {
            if(j.contains("state") && j.contains("name")) {
                if (j["name"] == modelName) {
                    _modelMetadata[modelName]["state"] = j["state"];
                    break;
                }
            }
        }
        this->_model_meta_dump = _modelMetadata[modelName].dump();
        return this->_model_meta_dump.c_str();
    }

    const char* ListModels() override
    {
        HRESULT hr = S_OK;
        std::unique_lock<std::mutex> lock(_metadata_mutex);
        nlohmann::json repo_json;
        std::string index_buffer;
        CHECK_FAIL_MSG(_getModelIndex(index_buffer),nullptr, "getting Triton Server Index failed");
        CHECKIF_MSG(nlohmann::json::accept(index_buffer) == false,nullptr,"JSON returned from Triton Server Index is invalid");
        repo_json = nlohmann::json::parse(index_buffer);
        for (const auto& j : repo_json) {
            if(j.contains("state") && j.contains("name")) {
                     std::string name = j["name"];
                    _modelMetadata[name.c_str()]["state"] = j["state"];
            }
        }
        this->_list_models_dump = _modelMetadata.dump();
        return this->_list_models_dump.c_str();
    }

    const char* GetMetrics() override {
        HRESULT hr = S_OK;
        std::string index_buffer;
        CHECK_FAIL_MSG(_getMetrics(index_buffer),nullptr, "getting Triton Metrics failed.");
        this->_prometheus_formatted_string_metrics = std::move(index_buffer);
        return this->_prometheus_formatted_string_metrics.c_str();
    }

    HRESULT GetStatus(IBuffer** ppObj) override
    {
        HRESULT hr = S_OK;
        CHECKNULL(ppObj, E_POINTER);
        CHECKNULL(this->_server, E_INVALID_STATE);

        TRITONSERVER_Message* message;
        const char *buffer;
        size_t sz;
        ComPtr<IBuffer> status;

        CHECK_TRITON_RES(TRITONSERVER_ServerMetadata(this->_server, &message));
        CHECK_TRITON_RES(TRITONSERVER_MessageSerializeToJson(message, &buffer, &sz));

        CHECKHR(Buffer::Create(status.AddressOf(), sz));
        CHECKIF(status->Data() != memcpy(status->Data(), buffer, sz), E_FAIL);
        *ppObj = status.Detach();

    Cleanup:
        TRITONSERVER_MessageDelete(message);
        return hr;
    }

    const char* GetModelStatus(const char* modelName) override {
        HRESULT hr = S_OK;
        std::unique_lock<std::mutex> lock(_metadata_mutex);
        CHECKNULL(modelName, nullptr);
        if(!_modelMetadata.contains(modelName))
        {
            TraceVerbose("Could not find metadata for model %s", modelName);
            _model_status_dump = "UNKNOWN";
            return _model_status_dump.c_str();
        }
        _model_status_dump = _modelMetadata[modelName]["state"];
        TraceInfo("Model %s status is %s", modelName, _model_status_dump.c_str());
        return _model_status_dump.c_str();
    }

    HRESULT ProcessRequest(IInferenceRequest* request) override
    {
        HRESULT hr = S_OK;
        CHECKNULL(request, E_INVALIDARG);
        ComPtr<ITritonRequest> triton_request = ComPtr<IInferenceRequest>(request).QueryInterface<ITritonRequest>();
        CHECKNULL(triton_request, E_NOINTERFACE);

        CHECK_TRITON_RES(TRITONSERVER_ServerInferAsync(this->_server, triton_request->Get(), nullptr));
        CHECKHR(triton_request->WaitForRequestToComplete());

    Cleanup:
        return hr;
    }

    TRITONSERVER_Server* Get() override
    {
        return _server;
    }

private:
    HRESULT Initialize(const char* modelRepoPath, const char* tritonServerPath)
    {
        HRESULT hr = S_OK;
        int counter = 0;
        nlohmann::json repo_json;
        _load_model_job_queue.SetProcessor([&](std::string modelName)
        {
            return this->_LoadModel(modelName.c_str());
        });
        _load_model_job_queue.SetName("TritonModelLoadJobQueue");
        _load_model_job_queue.Start();
        CHECKNULL(modelRepoPath, E_INVALIDARG);
        CHECKNULL(tritonServerPath, E_INVALIDARG);
        this->_tritonServerPath = fs::path(tritonServerPath);

        TRITONSERVER_ServerOptions* serverOptions = nullptr;
        CHECK_TRITON_RES(TRITONSERVER_ServerOptionsNew(&serverOptions));
        CHECK_TRITON_RES(TRITONSERVER_ServerOptionsSetModelRepositoryPath(serverOptions, modelRepoPath));
        CHECK_TRITON_RES(TRITONSERVER_ServerOptionsSetStrictReadiness(serverOptions, true));
        CHECK_TRITON_RES(TRITONSERVER_ServerOptionsSetStrictModelConfig(serverOptions, true));
        CHECK_TRITON_RES(TRITONSERVER_ServerOptionsSetModelControlMode(serverOptions, TRITONSERVER_MODEL_CONTROL_EXPLICIT));
        CHECK_TRITON_RES(TRITONSERVER_ServerOptionsSetBackendDirectory(serverOptions,  (this->_tritonServerPath / "backends").c_str()));
        CHECK_TRITON_RES(TRITONSERVER_ServerOptionsSetRepoAgentDirectory(serverOptions,  (this->_tritonServerPath / "repoagents").c_str()));
	CHECK_TRITON_RES(TRITONSERVER_ServerOptionsSetModelLoadThreadCount(serverOptions, 1));
        // TODO: Custom TritonLogListener
        CHECK_TRITON_RES(TRITONSERVER_ServerOptionsSetLogError(serverOptions, 0));
        CHECK_TRITON_RES(TRITONSERVER_ServerOptionsSetLogWarn(serverOptions, 0));
        CHECK_TRITON_RES(TRITONSERVER_ServerOptionsSetLogInfo(serverOptions, 0));
        CHECK_TRITON_RES(TRITONSERVER_ServerOptionsSetLogVerbose(serverOptions, 0));
        
        TraceVerbose("Creating new server instance.  Repo path = %s, Server Path = %s", modelRepoPath, tritonServerPath);
        CHECK_TRITON_RES(TRITONSERVER_ServerNew(&this->_server, serverOptions));

        do
        {
            bool live, ready;
            CHECK_TRITON_RES(TRITONSERVER_ServerIsLive(this->_server, &live));
            CHECK_TRITON_RES(TRITONSERVER_ServerIsReady(this->_server, &ready));
            if(live && ready)
            {
                break;
            }

            ThreadSleep(250);
            counter++;
        } while (counter < 10);
        TRITONSERVER_Message* repoMetaData;
        const char *buffer;
        size_t sz;
        CHECKIF(counter == 10, E_INVALID_STATE);
        /* Preload model name and state, of all models in model repository(in lfv terms model registry),
        0 passed to indicate to load metadata on all models.
        */
        /* ModelReadyState in Triton options

        enum ModelReadyState

        Readiness state for models.
        ModelReadyState::MODEL_UNKNOWN = 0
            The model is in an unknown state. The model is not available for inferencing.
        ModelReadyState::MODEL_READY = 1
            The model is ready and available for inferencing.
        ModelReadyState::MODEL_UNAVAILABLE = 2
            The model is unavailable, indicating that the model failed to load or has been implicitly or explicitly unloaded. The model is not available for inferencing.
        ModelReadyState::MODEL_LOADING = 3
            The model is being loaded by the inference server. The model is not available for inferencing.
        ModelReadyState::MODEL_UNLOADING = 4
            The model is being unloaded by the inference server. The model is not available for inferencing.

        NOTE: TRITONSERVER_ServerModelIndex provides ModelReadyState, TRITONSERVER_ServerModelMetadata does NOT.
        */
        CHECK_TRITON_RES(TRITONSERVER_ServerModelIndex(this->_server, 0,&repoMetaData));
        CHECK_TRITON_RES(TRITONSERVER_MessageSerializeToJson(repoMetaData, &buffer, &sz))
        repo_json = nlohmann::json::parse(std::string(buffer));
        for (const auto& j : repo_json) {
            if (j.contains("name")) {
                nlohmann::json meta;
                // Triton state is unkown for unloaded models;
                std::string m_name = j["name"];
                meta["name"] = m_name;
                meta["state"] = "UNKNOWN";
                this->_modelMetadata[m_name.c_str()] = meta;
                this->_loaded_models[m_name] = false;
            }
        }

    Cleanup:
        TRITONSERVER_ServerOptionsDelete(serverOptions);
        return hr;
    }

    fs::path _tritonServerPath;
    TRITONSERVER_Server* _server = nullptr;
    nlohmann::json _modelMetadata;
    std::string _list_models_dump;
    std::string _model_meta_dump;
    std::string _model_status_dump;
    std::string _prometheus_formatted_string_metrics;
    std::unordered_map<std::string, bool> _loaded_models;
    MultiThreadedJobQueue<std::string> _load_model_job_queue;
    std::mutex _metadata_mutex;
};

static std::mutex instance_mtx;
static std::map<size_t, ComPtr<ITritonServer>> instances;

DLLAPI HRESULT CreateTritonInferenceServer(IInferenceServer** ppObj, const char* modelRepoPath, const char* tritonServerPath, bool unique)
{
    std::scoped_lock lk(instance_mtx);

    HRESULT hr = S_OK;
    CHECKNULL(ppObj, E_POINTER);

    if(unique == false)
    {
        CHECKNULL_OR_EMPTY(modelRepoPath, E_INVALIDARG);
        CHECKNULL_OR_EMPTY(tritonServerPath, E_INVALIDARG);

        std::hash<std::string> hash_fn;
        size_t model_repo_hash = hash_fn(modelRepoPath);
        size_t install_hash = hash_fn(tritonServerPath);
        size_t combined = model_repo_hash + install_hash;

        // Instance hasn't been created yet
        if(instances.find(combined) == instances.end())
        {
            ComPtr<ITritonServer> server;
            CHECKHR(TritonServer::Create(server.AddressOf(), modelRepoPath, tritonServerPath));
            instances[combined] = server;
        }

        instances[combined].AddRef();
        *ppObj = instances[combined].Ptr();
        return hr;
    }

    ComPtr<ITritonServer> server;
    CHECKHR(TritonServer::Create(server.AddressOf(), modelRepoPath, tritonServerPath));
    *ppObj = server.Detach();
    return hr;
}

DLLAPI void ReleaseTritonInferenceServers()
{
    std::scoped_lock lk(instance_mtx);
    instances.clear();
}
