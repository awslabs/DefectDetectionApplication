#include <mutex>
#include <sstream>
#include <fstream>
#include <string>
#include <thread>
#include <queue>
#include <list>
#include <regex>

#include <nlohmann/json.hpp>
#include <aws/core/Aws.h>

#include <Panorama/comptr.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/eventing.h>
#include <Panorama/app.h>
#include <Panorama/aws.h>
#include <env_vars.h>
#include <misc.h>

#include "device_env.h"

using namespace Panorama;

#define HEARTBEAT_INTERVAL 10000

class PanoramaApp : public UnknownImpl<IApp>
{
public:
    static HRESULT Create(IApp** ppObj, int argc, char** argv)
    {
        HRESULT hr = S_OK;
        CREATE_COM(PanoramaApp, ptr);
        CHECKHR(ptr->Initialize(argc, argv));
        *ppObj = ptr.Detach();
        return hr;
    }

    ~PanoramaApp()
    {
        COM_DTOR(PanoramaApp);
        _shutdown.Set();

        if(_heartbeatThread.joinable())
        {
            _heartbeatThread.join();
        }

        COM_DTOR_FIN(PanoramaApp);
    }

    HRESULT GetProperty(IProperty** ppObj, const char* property) override
    {
        HRESULT hr = S_OK;
        CHECKNULL(ppObj, E_POINTER);
        *ppObj = nullptr;

        CHECKNULL(property, E_INVALIDARG);
        if(_propertyDelegateChain.size() == 0)
        {
            return E_NOT_FOUND;
        }

        ComPtr<IProperty> prop;
        for(int32_t idx = 0; idx < _propertyDelegateChain.size(); idx++)
        {
            ComPtr<IPropertyDelegate> propertyDelegate = _propertyDelegateChain[idx];
            hr = propertyDelegate->GetProperty(prop.AddressOf(), property);
            if(SUCCEEDED(hr))
            {
                *ppObj = prop.Detach();
                return hr;
            }

            if(hr == E_NOT_FOUND)
            {
                continue;
            }

            CHECKHR(hr);
        }

        // Should be E_NOT_FOUND if all searches did not fail but did not find property
        return hr;
    }

    template<typename T>
    HRESULT GetTypedProperty(T** ppObj, const char* propertyName)
    {
        HRESULT hr;
        CHECKNULL(ppObj, E_POINTER);
        
        ComPtr<IProperty> property;
        hr = GetProperty(property.AddressOf(), propertyName);
        if(FAILED(hr))
        {
            TraceDebug("Property %s requested but could not be found", propertyName);
            return hr;
        }

        ComPtr<T> queryProp = property.QueryInterface<T>();
        CHECKNULL_MSG(queryProp, E_NOINTERFACE, "Property is not correct type");
        *ppObj = queryProp.Detach();

        return hr;
    }

    HRESULT GetStringProperty(IStringProperty** ppObj, const char* propertyName) override
    {
        return GetTypedProperty<IStringProperty>(ppObj, propertyName);
    }

    HRESULT GetIntegerProperty(IIntegerProperty** ppObj, const char* propertyName) override
    {
        return GetTypedProperty<IIntegerProperty>(ppObj, propertyName);
    }

    HRESULT GetFloatProperty(IFloatProperty** ppObj, const char* propertyName) override
    {
        return GetTypedProperty<IFloatProperty>(ppObj, propertyName);
    }

    HRESULT GetBooleanProperty(IBooleanProperty** ppObj, const char* propertyName) override
    {
        return GetTypedProperty<IBooleanProperty>(ppObj, propertyName);
    }

    HRESULT GetCredentialsAsJSON(IBuffer** ppObj) override
    {
        return _credentialProvider->GetCredentialsAsJSON(ppObj);
    }

    Aws::Auth::AWSCredentials GetAWSCredentials() override
    {
        return _credentialProvider->GetAWSCredentials();
    }

    std::shared_ptr<Aws::Auth::AWSCredentialsProvider> CredentialProvider() override
    {
        if(_credentialProvider != nullptr)
        {
            return _credentialProvider->CredentialProvider();
        }
        
        return nullptr;
    }

    HRESULT AddPropertyDelegate(IPropertyDelegate* propDelegate) override
    {
        HRESULT hr = S_OK;
        CHECKNULL(propDelegate, E_INVALIDARG);
        _propertyDelegateChain.push_back(propDelegate);
        return S_OK;
    }

    HRESULT RemovePropertyDelegate(IPropertyDelegate* propDelegate) override
    {
        HRESULT hr = S_OK;
        CHECKNULL(propDelegate, E_INVALIDARG);
        auto it = std::find(_propertyDelegateChain.begin(), _propertyDelegateChain.end(), propDelegate);
        if (it != _propertyDelegateChain.end()) 
        {
            _propertyDelegateChain.erase(it);
        }

        return hr;
    }

    HRESULT Synchronize(IPropertyCollection** ppObj) override
    {
        HRESULT hr = S_OK;
        CHECKNULL(ppObj, E_POINTER);

        ComPtr<IPropertyCollection> aggregated_change;
        CHECKHR(CreatePropertyCollection(aggregated_change.AddressOf()));

        auto property_delegates(_propertyDelegateChain);
        for(auto iter = property_delegates.begin(); iter != property_delegates.end(); iter++)
        {
            ComPtr<IPropertyCollection> changed_properties;
            CHECKHR(iter->Ptr()->Synchronize(changed_properties.AddressOf()));

            // append to the aggregated_change collection
            for(int32_t idx = 0; idx < changed_properties->Count(); idx++)
            {
                ComPtr<IProperty> property;
                CHECKHR(changed_properties->At(property.AddressOf(), idx));
                CHECKHR(aggregated_change->Add(property));
            }
        }

        *ppObj = aggregated_change.Detach();
        return hr;
    }

private:
    PanoramaApp() = default;

    HRESULT InitializeStandAloneClient()
    {
        TraceInfo("Configuring to run as a stand alone client");
        _credentialProvider = Panorama_Aws::DefaultCredentialProvider();
        return S_OK;
    }

    HRESULT IntiailizeLocalClient()
    {
        HRESULT hr = S_OK;
        std::string ip;
        int port;
        
        TraceInfo("Configuring to run as a local client connected to Mock Device");
        std::string nodeUid = GetEnvVar(NODE_UID_ENV, "");
        CHECKHR(GetMDSIp(ip, port, true));
        CHECKHR(CreateMDSClient(_mds_client.AddressOf(), ip.c_str(), port, nodeUid.c_str()));
        CHECKHR(_mds_client->AnnounceSelf());
        CHECKHR(CreateMDSCredentialProvider(_credentialProvider.AddressOf(), _mds_client));
        
        ComPtr<IPropertyDelegate> property_delegate;
        CHECKHR(CreateMDSPropertyDelegate(property_delegate.AddressOf(), _mds_client));
        CHECKHR(this->AddPropertyDelegate(property_delegate));
        StartHeartbeat();
        return hr;
    }

    HRESULT InitializePanoramaClient()
    {
        HRESULT hr = S_OK;
        TraceInfo("Configuring to run as a Panorama Device client");

        // Add file trace listener to generate app log
        std::string logFile = GetEnvVar(LOG_FILE_OVERRIDE_ENV, DEFAULT_LOG_FILE);
        TraceVerbose("Log file: %s", logFile.c_str());

        std::string max_size_str = GetEnvVar("LOG_FILE_SIZE", "100000000");
        std::string num_backup = GetEnvVar("LOG_FILE_BACKUP", "2");
        
        int32_t max_bytes = 100000000;
        int32_t num_backups = 2;

        try
        {
            max_bytes = std::stoi(max_size_str);
            num_backups = std::stoi(num_backup);
        }
        catch(...)
        {
            TraceWarning("Environment variable LOG_FILE_SIZE and/or LOG_FILE_BACKUP did not contain an integer.  Using default values of 100000000 and 2 respectively.");
        }

        CHECKHR(CreateFileTraceListener(_fileTraceListener.AddressOf(), logFile.c_str(), max_bytes, num_backups));
        CHECKHR(Tracer::AddTraceListener(_fileTraceListener));

        // Get the IP and Port of MDS
        int port;
        std::string ip;
        CHECKHR(GetMDSIp(ip, port));
        TraceVerbose("Port number for MDS is %d", port);
        TraceVerbose("MDS Ip = %s", ip.c_str());

        // Get the Node_Uuid
        std::string nodeUid = GetEnvVar(NODE_UID_ENV);
        CHECKIF_MSG(nodeUid.empty(), E_INVALID_STATE, NODE_UID_ENV" environment variable not found");
        TraceVerbose(NODE_UID_ENV" = %s", nodeUid.c_str());

        CHECKHR(CreateMDSClient(_mds_client.AddressOf(), ip.c_str(), port, nodeUid.c_str()));
        CHECKHR(_mds_client->AnnounceSelf());
        CHECKHR(CreateMDSCredentialProvider(_credentialProvider.AddressOf(), _mds_client));
        ComPtr<IPropertyDelegate> property_delegate;
        CHECKHR(CreateMDSPropertyDelegate(property_delegate.AddressOf(), _mds_client));
        CHECKHR(this->AddPropertyDelegate(property_delegate));
        StartHeartbeat();

        return hr;
    }

    HRESULT Initialize(int argc, char** argv)
    {
        HRESULT hr = S_OK;

        // Add the CLI property delegate to the chain if needed
        if(argc > 0 && argv != nullptr)
        {
            ComPtr<IPropertyDelegate> cliPropertyDelegate;
            CHECKHR(CreateCLIPropertyDelegate(cliPropertyDelegate.AddressOf(), argc, argv));
            CHECKHR(AddPropertyDelegate(cliPropertyDelegate));
        }

        switch(DetermineClientType())
        {
            case ClientType::StandAlone:
                CHECKHR(this->InitializeStandAloneClient());
                break;
            case ClientType::LocalClient:
                CHECKHR(this->IntiailizeLocalClient());
                break;
            case ClientType::PanoramaClient:
                CHECKHR(this->InitializePanoramaClient());
                break;
            default:
                return E_NOTIMPL;
        }

        CHECKHR(AddPredefinedPropertyDelegates());

        return hr;
    }

    void StartHeartbeat()
    {
        _heartbeatThread = std::thread([&]()
        {
            while (_shutdown.WaitFor(0) == false)
            {
                // todo: Give app developer ability to set error codes/status
                _mds_client->Heartbeat("NONE", "ACTIVE");
                _shutdown.WaitFor(HEARTBEAT_INTERVAL);
            }
        });
    }

    HRESULT AddPredefinedPropertyDelegates()
    {
        HRESULT hr = S_OK;
        ComPtr<IStringProperty> delegates;
        ComPtr<IStringProperty> delegates_file;
        std::string contents;

        if(SUCCEEDED(this->GetStringProperty(delegates.AddressOf(), "PropertyDelegates")))
        {
            // Read direct from command line
            contents = delegates->Get();
        }
        else if(SUCCEEDED(this->GetStringProperty(delegates_file.AddressOf(), "PropertyDelegatesFile")))
        {
            // Read the file with the property delegates
            std::ifstream fstream(delegates_file->Get());
            CHECKIF_MSG(fstream.is_open() == false, E_FAIL, "Could not open PropertyDelegates file");
            std::stringstream ss;
            ss << fstream.rdbuf();
            contents = ss.str();
        }
        else
        {
            return S_FALSE;
        }

        CHECKIF_MSG(nlohmann::json::accept(contents) == false, E_FAIL, "PropertyDelegates files does not contain JSON");
        nlohmann::json jObj = nlohmann::json::parse(contents);

        for (nlohmann::json::iterator iter = jObj.begin(); iter != jObj.end(); iter++)
        {
            nlohmann::json delegateToAdd = *iter;
            if(ValidateJsonProperty<const char*>(delegateToAdd, "type") == false)
            {
                TraceError("Entry in property delegate %s is not valid", delegateToAdd.dump().c_str());
                continue;
            }

            // Possible entries are enumerated here 
            HRESULT add_delegate_hr;
            std::string delegateType = delegateToAdd["type"];
            ComPtr<IPropertyDelegate> propertyDelegate;
            if(delegateType.compare("ggv2_local_shadow") == 0)
            {
                TraceError("GGv2 Has not yet been ported");
                return E_NOTIMPL;
                // if(
                //     ValidateJsonProperty<const char*>(delegateToAdd, "thingName") &&
                //     ValidateJsonProperty<const char*>(delegateToAdd, "shadowName", false)
                // )
                // {
                //     std::string thingName = delegateToAdd["thingName"];
                //     std::string shadowName = delegateToAdd.contains("shadowName") ? delegateToAdd["shadowName"] : "";

                //     CHECKHR(CreateGGv2LocalShadowPropertyDelegate(propertyDelegate.AddressOf(), thingName.c_str(), shadowName.c_str()));
                // }
                // else
                // {
                //     TraceWarning("PropertyDelegate entry %s does not have correct metadata", delegateType.c_str());
                // }
            }
            else if(delegateType.compare("iot") == 0)
            {
                CHECKIF_MSG(ValidateJsonProperty<const char*>(delegateToAdd, "thingName") == false, E_INVALIDARG, "thingNamae isn't of type string or is not found");
                CHECKIF_MSG(ValidateJsonProperty<const char*>(delegateToAdd, "shadowName", false) == false, E_INVALIDARG, "shadowName is not of type string");
                CHECKIF_MSG(ValidateJsonProperty<const char*>(delegateToAdd, "region") == false, E_INVALIDARG, "region isn't of type string or is not found");

                std::string thingName = delegateToAdd["thingName"];
                std::string shadowName = delegateToAdd.contains("shadowName") ? delegateToAdd["shadowName"] : "";
                std::string region = delegateToAdd["region"];

                TraceInfo("Adding IoT Shadow Property Delegate %s:%s", thingName.c_str(), shadowName.c_str());
                CHECKHR(CreateIoTShadowPropertyDelegate(propertyDelegate.AddressOf(), thingName.c_str(), shadowName.c_str(), region.c_str(), _credentialProvider));
            }
            else if(delegateType.compare("s3") == 0)
            {
                CHECKIF_MSG(ValidateJsonProperty<const char*>(delegateToAdd, "bucket") == false, E_INVALIDARG, "bucket isn't of type string or is not found");
                CHECKIF_MSG(ValidateJsonProperty<const char*>(delegateToAdd, "key") == false, E_INVALIDARG, "key isn't of type string or is not found");
                CHECKIF_MSG(ValidateJsonProperty<const char*>(delegateToAdd, "region") == false, E_INVALIDARG, "region isn't of type string or is not found");

                std::string bucket = delegateToAdd["bucket"];
                std::string key = delegateToAdd["key"];
                std::string region = delegateToAdd["region"];

                TraceInfo("Adding S3 Property Delegate %s:%s", bucket.c_str(), key.c_str());
                CHECKHR(CreateS3PropertyDelegate(propertyDelegate.AddressOf(), bucket.c_str(), key.c_str(), region.c_str(), _credentialProvider));
            }
            else if(delegateType.compare("file") == 0)
            {
                CHECKIF_MSG(ValidateJsonProperty<const char*>(delegateToAdd, "path") == false, E_INVALIDARG, "path isn't of type string or is not found");
                
                std::string path = delegateToAdd["path"];
                TraceInfo("Adding File Property Delegate: %s", path.c_str());
                CHECKHR(CreateFilePropertyDelegate(propertyDelegate.AddressOf(), path.c_str()));
            }
            else
            {
                TraceWarning("Property delegate of type %s is unknown", delegateType.c_str());
            }

            add_delegate_hr = AddPropertyDelegate(propertyDelegate);
            if(FAILED(add_delegate_hr))
            {
                TraceError("Error adding property delegate %s", delegateType.c_str());
            }
        }

        return hr;
    }

    ManualResetEvent _shutdown;
    std::thread _heartbeatThread;
    ComPtr<IMDSClient> _mds_client;
    ComPtr<ITraceListener> _fileTraceListener;
    ComPtr<ICredentialProvider> _credentialProvider;
    std::vector<ComPtr<IPropertyDelegate>> _propertyDelegateChain;
};

DLLAPI HRESULT CreatePanoramaApp(IApp** ppObj, int argc, char** argv)
{
    return PanoramaApp::Create(ppObj, argc, argv);
}