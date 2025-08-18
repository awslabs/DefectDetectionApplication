#include <nlohmann/json.hpp>

#include <Panorama/comptr.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/gst.h>
#include <env_vars.h>

#include "../edge_app.h"

using namespace Panorama;

HRESULT StringToInt(int* pObj, const char* str) 
{
    CHECKNULL(pObj, E_POINTER);

    // Check if the remaining characters are digits
    for (int32_t idx = 0; idx < strlen(str); idx++) 
    {
        if(std::isdigit(str[idx]) == false)
        {
            return E_INVALIDARG;
        }
    }

    *pObj = atoi(str);
    return S_OK;
}

class EdgeAppConfig_v1 : public UnknownImpl<IEdgeAppConfig>
{
public:
    static HRESULT Create(IEdgeAppConfig** ppObj, IApp* app)
    {
        HRESULT hr = S_OK;
        CREATE_COM(EdgeAppConfig_v1, ptr);
        CHECKHR(ptr->Initialize(app));
        *ppObj = ptr.Detach();
        return hr;
    }

    ~EdgeAppConfig_v1()
    {
        COM_DTOR_FIN(EdgeAppConfig_v1);
    }

    int32_t GstLogLevel() override
    {
        return _gstLogLevel;
    }

    int32_t HeartbeatInterval() override
    {
        return _heartbeat_interval;
    }

    HRESULT AppContext(IApp** ppObj) override
    {
        CHECKNULL(ppObj, E_POINTER);
        _app.AddRef();
        *ppObj = _app;
        return S_OK;
    }

private:
    HRESULT Initialize(IApp* app)
    {
        HRESULT hr = S_OK;
        CHECKNULL(app, E_INVALIDARG);
        _app = app;

        // Set environment variables
        ComPtr<IStringProperty> envVar;
        if(SUCCEEDED(app->GetStringProperty(envVar.AddressOf(), "environment")))
        {
            std::string env_var_string = envVar->Get();
            if(nlohmann::json::accept(env_var_string) == false)
            {
                TraceError("Environment variables was not in the correct format, see documentation for the correct format");
                return E_INVALIDARG;
            }

            nlohmann::json obj = nlohmann::json::parse(env_var_string);
            for(auto& elem : obj.items())
            {
                std::string var = elem.key();
                std::string value = elem.value();
                TraceInfo("Setting %s to %s", var.c_str(), value.c_str());
                SetEnvVar(var, value);
            } 
        }

        // Get GstLogLevel
        std::string gstLogLevel = GetEnvVar("GST_DEBUG");
        if(gstLogLevel.empty() == false)
        {
            CHECKHR_MSG(StringToInt(&_gstLogLevel, gstLogLevel.c_str()), "GST_DEBUG is not a positive integer");
            CHECKIF_MSG(_gstLogLevel < 0 || _gstLogLevel > 9, E_OUTOFRANGE, "GST_DEBUG is out of range [0-9]");
        }
        else
        {
            _gstLogLevel = TraceLevelToGstLevel(Tracer::GetTraceLevel());
        }

        // Get the heartbeat interval
        ComPtr<IIntegerProperty> heartbeat_interval;
        if(FAILED(app->GetIntegerProperty(heartbeat_interval.AddressOf(), "heartbeat_interval")))
        {
            TraceInfo("Heartbeat interval was not specified, deafulting to 10 seconds");
            _heartbeat_interval = 10000;
        }
        else
        {
            _heartbeat_interval = heartbeat_interval->Get();
        }

        return hr;
    }

    ComPtr<IApp> _app;
    int32_t _gstLogLevel = 0;
    int32_t _heartbeat_interval = 0;
};

DLLAPI HRESULT CreateEdgeAppConfig_v1(IEdgeAppConfig** ppObj, IApp* app)
{
    return EdgeAppConfig_v1::Create(ppObj, app);
}