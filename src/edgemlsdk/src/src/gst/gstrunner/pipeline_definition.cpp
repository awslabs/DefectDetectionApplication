#include <string>

#include <Panorama/gst.h>
#include <Panorama/apidefs.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/comptr.h>

#include <collection_base.h>

using namespace Panorama;

class PipelineDefinition : public UnknownImpl<IPipelineDefinition>
{
public:
    static HRESULT Create(IPipelineDefinition** ppObj, const char* id, const char* definition)
    {
        HRESULT hr = S_OK;
        CHECKNULL(id, E_INVALIDARG);
        CHECKNULL(definition, E_INVALIDARG);

        CREATE_COM(PipelineDefinition, ptr);
        ptr->_id = id;
        ptr->_definition = definition;

        *ppObj = ptr.Detach();
        return hr;
    }

    ~PipelineDefinition()
    {
        COM_DTOR_FIN(PipelineDefinition);
    }

    const char* Id() override
    {
        return _id.c_str();
    }

    const char* GetDefinition() override
    {
        return _definition.c_str();
    }

private:
    PipelineDefinition() = default;

    std::string _id;
    std::string _definition;
};

class PipelineDefinitionCollection : public CollectionBase<IPipelineDefinitionCollection, IPipelineDefinition>
{
public:
    static HRESULT Create(IPipelineDefinitionCollection** ppObj)
    {
        HRESULT hr = S_OK;
        CREATE_COM(PipelineDefinitionCollection, ptr);
        *ppObj = ptr.Detach();
        return hr;
    }

    ~PipelineDefinitionCollection()
    {
        COM_DTOR_FIN(PipelineDefinitionCollection);
    }
};

DLLAPI HRESULT CreatePipelineDefinition(IPipelineDefinition** ppObj, const char* id, const char* definition)
{
    return PipelineDefinition::Create(ppObj, id, definition);
}

DLLAPI HRESULT CreatePipelineDefinitionCollection(IPipelineDefinitionCollection** ppObj)
{
    return PipelineDefinitionCollection::Create(ppObj);
}