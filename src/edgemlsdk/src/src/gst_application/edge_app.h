#ifndef __EDGE_APP_H__
#define __EDGE_APP_H__

#include <nlohmann/json.hpp>
#include <Panorama/apidefs.h>
#include <Panorama/comobj.h>
#include <Panorama/app.h>
#include <Panorama/gst.h>
#include <Panorama/gst_application.h>

namespace Panorama
{
    DEF_INTERFACE(IEdgeAppConfig, "{DBD0B386-7F7D-4A85-970B-6C8873E88534}", IUnknownAlias)
    {
        virtual int32_t GstLogLevel() = 0;
        virtual HRESULT AppContext(IApp** ppObj) = 0;
        virtual int32_t HeartbeatInterval() = 0;
    };

    DEF_INTERFACE(IEdgeApp, "{7C85ABBB-9C72-444B-8C23-D236ECA5F48B}", IUnknownAlias)
    {
        virtual HRESULT GetMessageBroker(IMessageBroker** ppObj) = 0;
        virtual HRESULT Start() = 0;
        virtual HRESULT Stop() = 0;
    };

    typedef HRESULT (*PluginLoader)(IAppPlugin** ppObj, PluginDescriptor& descriptor);

    DLLAPI HRESULT CreateEdgeAppConfig_v1(IEdgeAppConfig** ppObj, IApp* app);
    DLLAPI HRESULT CreateEdgeApp(IEdgeApp** ppObj, IEdgeAppConfig* config, PluginLoader loader = nullptr);
    DLLAPI HRESULT LoadAppPlugin(IAppPlugin** ppObj, const PluginDescriptor& plugin_descriptor);
}

#endif