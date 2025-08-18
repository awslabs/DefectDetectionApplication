#ifndef __GST_APPLICATION_H__
#define __GST_APPLICATION_H__

#include <Panorama/apidefs.h>
#include <Panorama/comobj.h>
#include <Panorama/properties.h>
#include <Panorama/credentials.h>
#include <Panorama/gst.h>
#include <Panorama/message_broker.h>

namespace Panorama
{
    DEF_INTERFACE(IAppPlugin, "{906B0ECC-1C59-47E5-AAF4-1F1C8FBD8B3E}", IUnknownAlias)
    {
        virtual HRESULT Initialize(IApp* app, IMessageBroker* message_broker) = 0;
        virtual HRESULT OnPipelineError(IPipelineError* error) = 0;
        virtual HRESULT OnPropertiesChanged(IPropertyCollection* changed_properties) = 0;
        virtual HRESULT Shutdown() = 0;
        virtual const char* Id() = 0;
    };

    enum class PluginType
    {
        Cpp = 0,
        Python
    };

    struct PluginDescriptor
    {
        PluginType Type;
        std::string Location;
    };

    DLLAPI HRESULT EmbedAppPlugin(IAppPlugin** ppObj, const char* moduleName, const char* className);
}

#endif