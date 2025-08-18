#include <dlfcn.h>
#include <map>
#include <mutex>

#include <Panorama/flowcontrol.h>
#include <Panorama/python.h>

#include <misc.h>

#include "edge_app.h"

using namespace Panorama;

class PluginLibrary
{
public:
    PluginLibrary() = default;

    ~PluginLibrary()
    {
        if(Handle != nullptr)
        {
            TraceInfo("Closing loaded library: %s", LibraryName.c_str());
            dlclose(Handle);
            Handle = nullptr;
        }
    }

    void* Handle = nullptr;
    std::string LibraryName;
};

static std::map<std::string, std::shared_ptr<PluginLibrary>> plugin_libraries;

typedef HRESULT (*CreatePlugin)(IAppPlugin** ppObj);
HRESULT LoadCppPlugin(IAppPlugin** ppObj, const char* shared_library)
{
    static std::mutex mtx;

    std::lock_guard<std::mutex> lk(mtx);
    HRESULT hr = S_OK;
    CHECKNULL(ppObj, E_POINTER);
    CHECKNULL_OR_EMPTY(shared_library, E_INVALIDARG);

    if(plugin_libraries.find(shared_library) == plugin_libraries.end())
    {
        std::shared_ptr<PluginLibrary> lib = std::shared_ptr<PluginLibrary>(new (std::nothrow) PluginLibrary());
        CHECKNULL(lib, E_OUTOFMEMORY);

        lib->Handle = dlopen(shared_library, RTLD_NOW);
        lib->LibraryName = shared_library;
        CHECKNULL_MSG(lib->Handle, E_FAIL, "%s", dlerror());
        plugin_libraries[shared_library] = lib;
    }

    dlerror();

    CreatePlugin factory = (CreatePlugin)dlsym(plugin_libraries[shared_library]->Handle, "CreateAppPlugin");
    const char *dlsym_error = dlerror();
    if (dlsym_error) 
    {
        TraceError("%s", dlsym_error);
        return E_FAIL;
    }

    return factory(ppObj);
};

HRESULT LoadPythonPlugin(IAppPlugin** ppObj, const char* module)
{
    HRESULT hr = S_OK;
    std::vector<std::string> strs = SplitString(module, ';');
    CHECKIF_MSG(strs.size() != 2, E_INVALIDARG, "Plugin location for python plugins should be formatted as '<module>;<class>'");
    ComPtr<IAppPlugin> plugin; 
    CHECKHR(EmbedAppPlugin(plugin.AddressOf(), strs[0].c_str(), strs[1].c_str()));
    *ppObj = plugin.Detach();
    return hr;
}

DLLAPI HRESULT LoadAppPlugin(IAppPlugin** ppObj, const PluginDescriptor& plugin_descriptor)
{
    switch (plugin_descriptor.Type)
    {
    case PluginType::Cpp:
        return LoadCppPlugin(ppObj, plugin_descriptor.Location.c_str());
    case PluginType::Python:
        return LoadPythonPlugin(ppObj, plugin_descriptor.Location.c_str());
    default:
        return E_NOTIMPL;
    }
}