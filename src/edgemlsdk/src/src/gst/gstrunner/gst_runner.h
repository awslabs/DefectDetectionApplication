#ifndef __GST_RUNNER_H__
#define __GST_RUNNER_H__

#include <vector>
#include <string>

#include <Panorama/apidefs.h>
#include <Panorama/app.h>
#include <Panorama/gst.h>

namespace Panorama
{
    struct DynamicProperty
    {
        std::string PropertyName;
        std::string PluginName;
        ComPtr<IVariableExpansion> Variable;
        ComPtr<IGstElement> Element;
    };

    struct PipelineStructure
    {
        ComPtr<IGstElement> Pipeline;
        std::vector<DynamicProperty> DynamicProperties;
    };

    DLLAPI HRESULT CreateVariableExpansion(IVariableExpansion** ppObj, IStringProperty* property, ICredentialProvider* cred_provider);
    DLLAPI HRESULT ExpandPipeline(std::string* pObj, PipelineStructure* structure, const char* pipelineDefinition);
    DLLAPI HRESULT GetPipelineStructure(PipelineStructure* pObj, const char* pipelineDefinition, IApp* propertyDelegate);
}

#endif