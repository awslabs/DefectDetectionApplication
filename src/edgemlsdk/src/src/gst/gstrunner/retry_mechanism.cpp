#include <thread>
#include <mutex>
#include <iostream>
#include <list>
#include <cmath>

#include <gst/gst.h>
#include <nlohmann/json.hpp>

#include <Panorama/comptr.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/chrono.h>
#include <Panorama/gst.h>
#include <Panorama/eventing.h>

#include <scheduling.h>

using namespace Panorama;

struct RetryContext 
{
    ComPtr<IPipeline> Pipeline; 
    ComPtr<IPipelineError> Error;
    int32_t RetryCount;
    ManualResetEvent RetrySucceedEvent;
    bool RetrySucceed;
    bool RetryEnd;
    std::thread RetryThread;
};

HRESULT GetRetryConfiguration(IPipelineErrorCollection** ppErr, RetryMode* mode, int32_t* min_delay, int32_t* max_delay, float* increment, IStringProperty* retryProperty)
{
    HRESULT hr = S_OK;

    CHECKNULL(mode, E_POINTER);
    CHECKNULL(ppErr, E_POINTER);
    CHECKNULL(min_delay, E_POINTER);
    CHECKNULL(max_delay, E_POINTER);
    CHECKNULL(increment, E_POINTER);

    ComPtr<IPipelineErrorCollection> error_collection;
    CHECKHR(CreatePipelineErrorCollection(error_collection.AddressOf()));

    // Get retry configuration from Json
    std::string retryStr = retryProperty->Get();
    CHECKIF(nlohmann::json::accept(retryStr) == false, E_INVALIDARG);
    nlohmann::json elem = nlohmann::json::parse(retryStr);

    // set _retry_mode 
    if(elem.contains("Mode") && (elem["Mode"]=="linear" || elem["Mode"]=="exponential"))
    {
        *mode = (elem["Mode"] == "exponential") ? RetryMode::Exponential : RetryMode::Linear;    
    }
    else
    {
        *mode = RETRY_MODE_DEFAULT;
        TraceWarning("Retry Mode should be linear/exponential. Set to default retry mode: Linear Mode");
    }

    // set _retry_msg
    if(elem.contains("Messages"))
    {
        for(const auto& msg : elem["Messages"])
        {
            CHECKIF((msg.contains("Type") && msg.contains("Domain") && msg.contains("Code")) == false, E_INVALIDARG);
            CHECKIF((msg["Type"].is_number() && msg["Domain"].is_number() && msg["Code"].is_number()) == false, E_INVALIDARG);
            ComPtr<IPipelineError> pipelineError;
            CreatePipelineError(pipelineError.AddressOf(), msg["Type"].get<int32_t>(), msg["Domain"].get<int32_t>(), msg["Code"].get<int32_t>());
            error_collection->Add(pipelineError);
        }
    }
    else
    {
        ComPtr<IPipelineError> pipelineError;
        CreatePipelineError(pipelineError.AddressOf(), MESSAGE_TYPE_DEFAULT, int(ERROR_DOMAIN_DEFAULT), ERROR_CODE_DEFAULT);
        error_collection->Add(pipelineError);
        TraceWarning("Set to default retry message: Error");
    }

    *min_delay = (elem.contains("Min") && elem["Min"].is_number()) ? elem["Min"].get<int32_t>() : MIN_DEFAULT;
    *max_delay = (elem.contains("Max") && elem["Max"].is_number()) ? elem["Max"].get<int32_t>() : MAX_DEFAULT;

    if(elem.contains("Increment") && elem["Increment"].is_number())
    {
        *increment = elem["Increment"].get<float>();
    }
    else
    {
        TraceWarning("Increment should be a float. Set to default Increment value");
        *increment = (*mode == RetryMode::Linear) ? LINEAR_INCREMENT_DEFAULT : EXPO_INCREMENT_DEFAULT;
    }

    *ppErr = error_collection.Detach();
    return hr;
}

class RetryMechanism : public UnknownImpl<IPipelineEventHandler>
{
public:
    static HRESULT CreateDefault(IPipelineEventHandler** ppObj)
    {
        HRESULT hr = S_OK;
        ComPtr<IPipelineErrorCollection> error_collection;
        CHECKHR(CreatePipelineErrorCollection(error_collection.AddressOf()));

        ComPtr<IPipelineError> pipelineError;
        CHECKHR(CreatePipelineError(pipelineError.AddressOf(), MESSAGE_TYPE_DEFAULT, int(ERROR_DOMAIN_DEFAULT), ERROR_CODE_DEFAULT));
        error_collection->Add(pipelineError);

        return RetryMechanism::Create(ppObj, RETRY_MODE_DEFAULT, MIN_DEFAULT, MAX_DEFAULT, LINEAR_INCREMENT_DEFAULT, error_collection);
    }

    static HRESULT CreateFromProperty(IPipelineEventHandler** ppObj, IStringProperty* property)
    {
        HRESULT hr = S_OK;

        RetryMode mode;
        int32_t min_delay, max_delay;
        float increment;
        ComPtr<IPipelineErrorCollection> error_collection;
        CHECKHR(GetRetryConfiguration(error_collection.AddressOf(), &mode, &min_delay, &max_delay, &increment, property));
        return RetryMechanism::Create(ppObj, mode, min_delay, max_delay, increment, error_collection);
    }

    static HRESULT Create(IPipelineEventHandler** ppObj, RetryMode retryMode, int32_t minDelayInMs, int32_t maxDelayInMs, float increment, IPipelineErrorCollection* errorCollection)
    {
        HRESULT hr = S_OK;
        CHECKNULL(ppObj, E_POINTER);
        CHECKIF(minDelayInMs < 0, E_INVALIDARG);
        CHECKIF(maxDelayInMs < 0, E_INVALIDARG);
        CHECKIF(maxDelayInMs < minDelayInMs, E_INVALIDARG);
        CHECKIF(increment < 0, E_INVALIDARG);
        CHECKNULL(errorCollection, E_INVALIDARG);
        
        CREATE_COM(RetryMechanism, ptr);
        ptr->_retry_mode = retryMode;
        ptr->_max_delay_in_ms = maxDelayInMs;
        ptr->_min_delay_in_ms = minDelayInMs;
        ptr->_increment = increment;
        ptr->_pipeline_error_collection = errorCollection;

        *ppObj = ptr.Detach();
        return hr;
    }

    ~RetryMechanism()
    {
        COM_DTOR(RetryMechanism);

        for (auto it = _retry_contexts.begin(); it != _retry_contexts.end(); ++it) 
        {
            RemovePipeline(_retry_contexts[it->first]->Pipeline);
        }

        COM_DTOR_FIN(RetryMechanism);
    }


    void OnError(IPipeline* sender, IPipelineError* error) override
    {
        std::string id = sender->Id();
        if(_retry_contexts.find(id) == _retry_contexts.end())
        {
            // RetryContext context;
            std::shared_ptr<RetryContext> context = std::make_shared<RetryContext>();
            context->Pipeline = sender;
            context->RetryCount = 0;
            context->RetrySucceed = false;
            context->RetryEnd = false;
            context->RetrySucceedEvent.Reset();

            int refCount = error->RefCount();
            context->Error = error;
            {
                std::lock_guard<std::mutex> lk(this->_contextLock);
                _retry_contexts[id] = std::move(context);
            }
            TraceInfo("Add Retry Mechanism to Pipeline [%s] %s", id.c_str(), sender->Definition());

            _retry_contexts[id]->RetryThread = std::thread([id, this]() 
            { 
                while(true)
                {
                    if(this->_retry_contexts[id]->RetryEnd == true)
                        break;
                    if(this->_retry_contexts[id]->RetrySucceed == true)
                        continue;

                    bool contains = false;
                    {
                        std::lock_guard<std::mutex> lk(this->_contextLock);
                        contains = _pipeline_error_collection->Contains(this->_retry_contexts[id]->Error);
                    }
                    
                    if(contains)
                    {
                        Retry(id);
                        bool succeed = this->_retry_contexts[id]->RetrySucceedEvent.WaitFor(1000);
                        if(succeed)
                        {
                            TraceInfo("Pipeline[%s] Retry Succeeds!", id.c_str());
                            this->_retry_contexts[id]->RetrySucceed = true;
                            this->_retry_contexts[id]->RetryCount = 0;
                        }
                    }
                }
            });
        }
        else
        {
            std::lock_guard<std::mutex> lk(_contextLock);
            _retry_contexts[id]->RetrySucceed = false;
            _retry_contexts[id]->Error = error;
        }
    }

    void OnStateChanged(IPipeline* sender, int32_t oldState, int32_t newState) override
    {
        if(newState == static_cast<int32_t>(GST_STATE_PLAYING))
        {
            std::string pipelineID = sender->Id();
            if(_retry_contexts.find(pipelineID)!=_retry_contexts.end())
            {
                std::lock_guard<std::mutex> lk(_contextLock);
                _retry_contexts[pipelineID]->RetrySucceedEvent.Set();
            }
        }
    }

    void OnRemovedFromPipeline(IPipeline* sender) override
    {
        RemovePipeline(sender);
    }

private:
    RetryMechanism() = default;

    HRESULT RemovePipeline(IPipeline* pipeline)
    {
        HRESULT hr = S_OK;
        std::string id = pipeline->Id();
        std::string definition = pipeline->Definition();
        CHECKIF(id.empty(), E_INVALIDARG);
        CHECKIF(definition.empty(), E_INVALIDARG);
        if(_retry_contexts.find(id) == _retry_contexts.end())
        {
            TraceWarning("Pipeline [%s] %s doesn't have a Retry Mechanism, no need to remove", id.c_str(), definition.c_str());
            return S_FALSE;
        }
        TraceInfo("Remove Retry Mechanism from Pipeline [%s] %s", id.c_str(), definition.c_str());

        _retry_contexts[id]->RetryEnd = true;
        if(_retry_contexts[id]->RetryThread.joinable())
        {
            _retry_contexts[id]->RetryThread.join();
        }
        _retry_contexts.erase(id);
        return hr;
    }

    int32_t CalculateDelayInMs(std::string pipelineID)
    {
        _retry_contexts[pipelineID]->RetryCount++;
        int32_t count = _retry_contexts[pipelineID]->RetryCount;
        if(count == 1)
        {
            return (_retry_mode == RetryMode::Linear) ? _min_delay_in_ms : _min_delay_in_ms+1;
        }
        int32_t delayInMs;
        if(_retry_mode == RetryMode::Linear)
        {
            delayInMs = _min_delay_in_ms + _increment * (count-1);
        }
        else
        {
            delayInMs = _min_delay_in_ms + pow(_increment, count-1);
        }

        if(delayInMs > _max_delay_in_ms)
        {
            _retry_contexts[pipelineID]->RetryCount--;
            return _max_delay_in_ms;
        }
        return delayInMs;
    }

    HRESULT Retry(std::string pipelineID)
    {
        HRESULT hr = S_OK;
        ComPtr<IPipeline> pipeline = _retry_contexts[pipelineID]->Pipeline;
        int32_t delayInMs = CalculateDelayInMs(pipelineID);
        TraceInfo("Retry after %d ms", delayInMs);
        ThreadSleep(delayInMs);
        TraceInfo("Restart pipeline [%s]", pipelineID.c_str());
        _retry_contexts[pipelineID]->RetrySucceedEvent.Reset();
        CHECKHR(pipeline->Restart());
        return hr;
    }

    std::mutex _contextLock;
    // retry context
    std::map<std::string, std::shared_ptr<RetryContext>> _retry_contexts;

    // retry mechanism configuration
    RetryMode _retry_mode;
    int32_t _min_delay_in_ms;
    int32_t _max_delay_in_ms;
    float _increment;
    ComPtr<IPipelineErrorCollection> _pipeline_error_collection;

    // callback functions
    std::function<void(IPipeline*, IPipelineError*)> _onErrorCb;
    std::function<void(IPipeline*, int32_t, int32_t)> _onStateChangeCb;
};

DLLAPI HRESULT CreateRetryMechanism(IPipelineEventHandler** ppObj, RetryMode retryMode, int32_t minDelayInMs, int32_t maxDelayInMs, float increment, IPipelineErrorCollection* errorCollection)
{
    return RetryMechanism::Create(ppObj, retryMode, minDelayInMs, maxDelayInMs, increment, errorCollection);
}

DLLAPI HRESULT CreateRetryMechanismFromProperty(IPipelineEventHandler** ppObj, IStringProperty* configuration)
{
    return RetryMechanism::CreateFromProperty(ppObj, configuration);
}

DLLAPI HRESULT CreateRetryMechanismDefault(IPipelineEventHandler** ppObj)
{
    return RetryMechanism::CreateDefault(ppObj);
}