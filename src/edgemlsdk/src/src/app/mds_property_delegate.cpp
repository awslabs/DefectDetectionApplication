#include <nlohmann/json.hpp>

#include <Panorama/comptr.h>
#include <Panorama/app.h>
#include <Panorama/flowcontrol.h>

#include <core/rest/rest.h>
#include <core/property/property_manager.h>

using namespace Panorama;

#define USER_AGENT "Node Runtime"

class MDSPropertyDelegate : public UnknownImpl<IPropertyDelegate>
{
public:
    static HRESULT Create(IPropertyDelegate** ppObj, IMDSClient* mds_client)
    {
        HRESULT hr = S_OK;
        CREATE_COM(MDSPropertyDelegate, ptr);
        CHECKHR(ptr->Initialize(mds_client));
        *ppObj = ptr.Detach();
        return hr;
    }

    ~MDSPropertyDelegate()
    {
        COM_DTOR(MDSPropertyDelegate);
    }

    HRESULT GetProperty(IProperty** ppObj, const char* property) override
    {
        return _propertyManager->GetProperty(ppObj, property);
    }

    HRESULT Synchronize(IPropertyCollection** ppObj) override
    {
        return CreatePropertyCollection(ppObj);
    }

private:
    MDSPropertyDelegate() = default;

    HRESULT Initialize(IMDSClient* mds_client)
    {
        HRESULT hr = S_OK;
        CHECKNULL(mds_client, E_INVALIDARG);
        _mds_client = mds_client;
        TraceVerbose("Initializing MDS Property Delegate");

        CHECKHR(PropertyManager::Create(_propertyManager.AddressOf(), "MDS"));
        CHECKHR(GetPorts());
        return hr;
    }

    HRESULT GetPorts()
    {
        HRESULT hr = S_OK;
        int32_t attempt = 0;
        int32_t maxAttempts = 5;

        for(; attempt < maxAttempts; attempt++)
        {
            ComPtr<IBuffer> buffer;
            CHECKHR(_mds_client->GetPorts(buffer.AddressOf()));

            // Parse the response to get the input parameters
            try
            {
                nlohmann::json ports = nlohmann::json::parse(buffer->AsString());
                if (ports.contains("inputPortList"))
                {
                    nlohmann::json inputs = ports["inputPortList"];
                    for (int idx = 0; idx < inputs.size(); idx++)
                    {
                        nlohmann::json entry = inputs.at(idx);
                        std::string name = entry["name"];
                        nlohmann::json val = entry["value"];
                        std::string str = val["dataList"][0];

                        if (entry["type"] == "STRING")
                        {
                            CHECKHR(_propertyManager->SetProperty(name.c_str(), str.c_str()));
                            TraceVerbose("Adding key %s with value %s", name.c_str(), str.c_str());
                        }
                        else if (entry["type"] == "INT32")
                        {
                            // todo: error check str is an actual number
                            CHECKHR(_propertyManager->SetProperty(name.c_str(), std::stoi(str)));
                        }
                        else if (entry["type"] == "FLOAT32")
                        {
                            // todo: error check str is an actual number
                            CHECKHR(_propertyManager->SetProperty(name.c_str(), std::stof(str)));
                        }
                        else if (entry["type"] == "BOOLEAN")
                        {
                            std::transform(str.begin(), str.end(), str.begin(),
                                [](unsigned char c) { return std::tolower(c); });

                            CHECKHR(_propertyManager->SetProperty<bool>(name.c_str(), strcmp("true", str.c_str()) == 0));
                        }
                    }

                    TraceInfo("Received input ports from MDS");
                    break;
                }
                else
                {
                    // Ports have not yet been announced, wait for a second and try again
                    TraceWarning("Ports do not appear to have been announced yet");
                    ThreadSleep(1000);
                }
            }
            catch (const std::exception &e)
            {
                TraceError("Failed to parse response from GetPorts: %s", e.what());
                return E_FAIL;
            }
            catch (...)
            {
                TraceError("Failed to parse response from GetPorts with non-standard exception");
                return E_FAIL;
            }
        }

        if(attempt == maxAttempts)
        {
            TraceError("Timed out waiting for ports from MDS");
            return E_FAIL;
        }

        return hr;
    }

    ComPtr<IMDSClient> _mds_client;
    ComPtr<PropertyManager> _propertyManager;
    std::string _nodeUid;
};

DLLAPI HRESULT CreateMDSPropertyDelegate(IPropertyDelegate** ppObj, IMDSClient* mds_client)
{
    return MDSPropertyDelegate::Create(ppObj, mds_client);
}