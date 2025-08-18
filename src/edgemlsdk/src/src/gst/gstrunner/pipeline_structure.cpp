#include <vector>
#include <string>
#include <tuple>

#include <Panorama/comptr.h>
#include <Panorama/flowcontrol.h>
#include <misc.h>
#include "gst_runner.h"
 
using namespace Panorama;

std::string GetPluginName(std::vector<std::string> properties)
{
    // Get the name of the plugin
    std::string pluginName;
    for(int32_t jdx = 1; jdx < properties.size(); jdx++)
    {
        std::vector<std::string> keyValue = SplitString(properties[jdx], '=');
        if(keyValue[0].compare("name") == 0)
        {
            pluginName = keyValue[1];
        }
    }

    return pluginName;
}

HRESULT FindVariables(std::vector<DynamicProperty>* pObj, const char* pipelineDefinition, IApp* app)
{
    HRESULT hr = S_OK;

    std::string canonicalDefinition = pipelineDefinition;

    // Deal with white spaces
    // todo, use regex to make this more robust
    FindAndReplace(canonicalDefinition, " ! ", "!");
    FindAndReplace(canonicalDefinition, " = ", "=");
    FindAndReplace(canonicalDefinition, "  ", " ");

    // Split the definition into the list of plugins
    std::vector<std::string> plugins = SplitString(canonicalDefinition, '!');

    // For each plugin
    for(int32_t idx = 0; idx < plugins.size(); idx++)
    {
        // Split into plugin and properties
        std::vector<std::string> properties = SplitString(plugins[idx], ' ');

        // Can skip first element of properties as it would be the
        // plugin registration name
        for(int32_t jdx = 1; jdx < properties.size(); jdx++)
        {
            std::vector<std::string> keyValue = SplitString(properties[jdx], '=');
            
            // plugin properties may not be key/value pairs
            if (keyValue.size() == 1)
            {
                continue;
            }

            // TODO: Add support for multiple key value plugin attributs
            // (e.g. caps filter)
            if (keyValue.size() != 2)
            {
                TraceWarning("Not processing variables in %s", properties[jdx].c_str());
                continue;
            }

            std::vector<std::string> varName = EncapsulatedStrings(keyValue[1], "\\$\\{", "\\}");
            if(varName.size() == 1)
            {
                TraceInfo("Found variable %s", varName[0].c_str());

                // Found a dynamic property
                std::string pluginName = GetPluginName(properties);
                CHECKIF_MSG(pluginName.empty(), E_FAIL, "Plugins with dynamic properties must have the name property set");

                // Create the dynamic property object
                DynamicProperty dynProperty = DynamicProperty();
                dynProperty.PropertyName = keyValue[0];
                dynProperty.PluginName = std::move(pluginName);

                // Get the property
                ComPtr<IStringProperty> property;
                CHECKHR(app->GetStringProperty(property.AddressOf(), varName[0].c_str()));
                CHECKHR(CreateVariableExpansion(dynProperty.Variable.AddressOf(), property, app));
                pObj->push_back(dynProperty);
            }
        }
    }

    return hr;
}

HRESULT CreateGstreamerPipeline(IGstElement** ppObj, std::string& launchString)
{
    GError* err = nullptr;
    GstElement* parsedElement = gst_parse_launch(launchString.c_str(), &err);
    if(err != nullptr)
    {
        TraceError("Error parsing pipeline \"%s\":  %s", launchString.c_str(), err->message);
        g_error_free(err);
        return E_INVALIDARG;
    }

    HRESULT hr = GstElementMakeCom(ppObj, parsedElement);
    if(FAILED(hr))
    {
        TraceError("Error parsing pipeilne description '%s' with error %s", launchString.c_str(), err == nullptr ? "<empty>" : err->message);
        return hr;
    }

    return hr;
}

DLLAPI HRESULT ExpandPipeline(std::string* pObj, PipelineStructure* structure, const char* pipelineDefinition)
{
    HRESULT hr = S_OK;
    CHECKNULL(pObj, E_POINTER);

    *pObj = pipelineDefinition;
    for (DynamicProperty property : structure->DynamicProperties)
    {
        ComPtr<IBuffer> expansionBuffer;
        CHECKHR(property.Variable->Expand(expansionBuffer.AddressOf()));
        std::string expansion = expansionBuffer->AsString();
        CHECKIF(expansion.empty(), E_FAIL);

        // escape special characters
        FindAndReplace(expansion, "\\", "\\\\");
        FindAndReplace(expansion, "\"", "\\\"");

        std::string variableText = "${" + std::string(property.Variable->Id()) + "}";
        FindAndReplace(*pObj, variableText.c_str(), expansion.c_str());
    }

    // Remove double quotes in capsfilter gst element
    // pattern 1: element ! "video/x-raw,format=GRAY8" ! element
    std::regex pattern1("!\\s*\"(.*?)\"\\s*"); 
    std::string output1 = std::regex_replace(pObj->c_str(), pattern1, "! $1 "); 
    // pattern 2: element ! capsfilter "caps=video/x-raw,format=GRAY8" ! element
    std::regex pattern2("!\\s*capsfilter\\s*\"(.*?)\"\\s*"); 
    std::string output2 = std::regex_replace(output1, pattern2, "! capsfilter $1 "); 
    *pObj = std::move(output2);

    return hr;
}

DLLAPI HRESULT GetPipelineStructure(PipelineStructure* pObj, const char* pipelineDefinition, IApp* app)
{
    HRESULT hr = S_OK;
    CHECKNULL(pObj, E_POINTER);
    CHECKNULL(app, E_INVALIDARG);

    // Step 1: Find all dynamic properties
    CHECKHR(FindVariables(&(pObj->DynamicProperties), pipelineDefinition, app));

    // Step 2: Expand those dynamic properties to get the fully qualified pipeline definition
    std::string initialExpansion;
    CHECKHR(ExpandPipeline(&initialExpansion, pObj, pipelineDefinition));
    TraceInfo("Expanded pipeline '%s'", pipelineDefinition);

    // Step 3: Create the gstreamer pipeline
    CHECKHR(CreateGstreamerPipeline(pObj->Pipeline.AddressOf(), initialExpansion));

    // Step 4: Get the plugins from the create gst_bin
    for(int32_t idx = 0; idx < pObj->DynamicProperties.size(); idx++)
    {
        // Get the gst element
        CHECKHR(GstElementMakeCom(pObj->DynamicProperties[idx].Element.AddressOf(), gst_bin_get_by_name(GST_BIN(pObj->Pipeline->Element()), pObj->DynamicProperties[idx].PluginName.c_str())));
        CHECKNULL_MSG(pObj->DynamicProperties[idx].Element, E_FAIL, "Could not find gstreamer element");
    }

    return hr;
}