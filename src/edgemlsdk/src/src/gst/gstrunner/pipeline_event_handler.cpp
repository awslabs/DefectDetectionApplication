#include <Panorama/gst.h>

using namespace Panorama;


HRESULT PipelineEventHandler::CreateOnError(IPipelineEventHandler** ppObj, std::function<void(IPipeline*, IPipelineError*)> cb)
{
    HRESULT hr = S_OK;
    CREATE_COM(PipelineEventHandler, ptr);
    ptr->_on_error_cb = std::move(cb);
    *ppObj = ptr.Detach();
    return hr;
}

HRESULT PipelineEventHandler::CreateOnStateChanged(IPipelineEventHandler** ppObj, std::function<void(IPipeline*, int32_t, int32_t)> cb)
{
    HRESULT hr = S_OK;
    CREATE_COM(PipelineEventHandler, ptr);
    ptr->_on_state_changed_cb = std::move(cb);
    *ppObj = ptr.Detach();
    return hr;
}

PipelineEventHandler::~PipelineEventHandler()
{
    COM_DTOR_FIN(PipelineEventHandler);
}

void PipelineEventHandler::OnError(IPipeline* sender, IPipelineError* error)
{
    if(_on_error_cb != nullptr)
    {
        _on_error_cb(sender, error);
    }
}

void PipelineEventHandler::OnStateChanged(IPipeline* sender, int32_t oldState, int32_t newState)
{
    if(_on_state_changed_cb != nullptr)
    {
        _on_state_changed_cb(sender, oldState, newState);
    }
}

void PipelineEventHandler::OnRemovedFromPipeline(IPipeline* sender)
{
    // todo
}