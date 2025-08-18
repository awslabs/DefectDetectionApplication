#include <Panorama/comptr.h>
#include <Panorama/gst_application.h>

using namespace Panorama;

class TestAppPlugin : public UnknownImpl<IAppPlugin>
{
public:
    static HRESULT Create(IAppPlugin** ppObj)
    {
        HRESULT hr = S_OK;
        CREATE_COM(TestAppPlugin, ptr);
        CHECKHR(ptr->Initialize());
        *ppObj = ptr.Detach();
        return hr;
    }

    ~TestAppPlugin()
    {
        COM_DTOR(TestAppPlugin);
        Shutdown();
        COM_DTOR_FIN(TestAppPlugin);
    }

    HRESULT Initialize(IApp* app, IMessageBroker* message_broker) override
    {
        return S_OK;
    }

    HRESULT OnPipelineError(IPipelineError* error) override
    {
        return E_NOTIMPL;
    }

    HRESULT OnPropertiesChanged(IPropertyCollection* changed_properties) override
    {
        return E_NOTIMPL;
    }

    HRESULT Shutdown() override
    {
        return E_NOTIMPL;
    }

    const char* Id() override
    {
        return _id.c_str();
    }

private:
    HRESULT Initialize()
    {
        _id = "test_plugin";
        return S_OK;
    }

    std::string _id;
};  

DLLAPI HRESULT CreateAppPlugin(IAppPlugin** plugin)
{
    return TestAppPlugin::Create(plugin);
}