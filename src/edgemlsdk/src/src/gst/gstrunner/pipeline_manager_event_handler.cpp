#include <Panorama/apidefs.h>
#include <Panorama/gst.h>

using namespace Panorama;

HRESULT PipelineManagerEventHandler::CreatePipelineAddedPreview(IPipelineManagerEventHandler** ppObj, std::function<bool(IPipelineDefinition*)> cb)
{
    HRESULT hr = S_OK;
    CREATE_COM(PipelineManagerEventHandler, ptr);
    ptr->_pipeline_added_preview_cb = std::move(cb);
    *ppObj = ptr.Detach();
    return hr; 
}

HRESULT PipelineManagerEventHandler::CreatePipelineAdded(IPipelineManagerEventHandler** ppObj, std::function<void(IPipeline*)> cb)
{
    HRESULT hr = S_OK;
    CREATE_COM(PipelineManagerEventHandler, ptr);
    ptr->_pipeline_added_cb = std::move(cb);
    *ppObj = ptr.Detach();
    return hr; 
}

HRESULT PipelineManagerEventHandler::CreatePipelineRemovedPreview(IPipelineManagerEventHandler** ppObj, std::function<bool(IPipeline*)> cb)
{
    HRESULT hr = S_OK;
    CREATE_COM(PipelineManagerEventHandler, ptr);
    ptr->_pipeline_removed_preview_cb = std::move(cb);
    *ppObj = ptr.Detach();
    return hr; 
}

HRESULT PipelineManagerEventHandler::CreatePipelineRemoved(IPipelineManagerEventHandler** ppObj, std::function<void(IPipeline*)> cb)
{
    HRESULT hr = S_OK;
    CREATE_COM(PipelineManagerEventHandler, ptr);
    ptr->_pipeline_removed_cb = std::move(cb);
    *ppObj = ptr.Detach();
    return hr; 
}

HRESULT PipelineManagerEventHandler::CreateDefinitionChangedPreview(IPipelineManagerEventHandler** ppObj, std::function<bool(IPipeline*, const char*)> cb)
{
    HRESULT hr = S_OK;
    CREATE_COM(PipelineManagerEventHandler, ptr);
    ptr->_pipeline_definition_change_preview_cb = std::move(cb);
    *ppObj = ptr.Detach();
    return hr; 
}

PipelineManagerEventHandler::~PipelineManagerEventHandler()
{
    COM_DTOR_FIN(PipelineManagerEventHandler);
}

bool PipelineManagerEventHandler::OnPipelineAddPreview(IPipelineDefinition* definition)
{
    return _pipeline_added_preview_cb == nullptr ?
        false :
        _pipeline_added_preview_cb(definition);
}

bool PipelineManagerEventHandler::OnPipelineRemovePreview(IPipeline* pipeline)
{
    return _pipeline_removed_preview_cb == nullptr ?
        false :
        _pipeline_removed_preview_cb(pipeline);
}

void PipelineManagerEventHandler::OnPipelineAdded(IPipeline* pipeline)
{
    if(_pipeline_added_cb != nullptr)
    {
        _pipeline_added_cb(pipeline);
    }
}

void PipelineManagerEventHandler::OnPipelineRemoved(IPipeline* pipeline)
{
    if(_pipeline_removed_cb != nullptr)
    {
        _pipeline_removed_cb(pipeline);
    }
}

bool PipelineManagerEventHandler::OnDefintionChangePreview(IPipeline* pipeline, const char* newDefinition)
{
    return _pipeline_definition_change_preview_cb == nullptr ?
        false :
        _pipeline_definition_change_preview_cb(pipeline, newDefinition);
}