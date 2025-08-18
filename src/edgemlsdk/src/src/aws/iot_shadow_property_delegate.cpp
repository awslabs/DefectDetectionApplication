#include <iostream>

#include <aws/core/Aws.h>
#include <aws/core/utils/Outcome.h>
#include <aws/iot-data/IoTDataPlaneClient.h>
#include <aws/iot-data/model/GetThingShadowRequest.h>
#include <aws/iot-data/model/GetThingShadowResult.h>
#include <aws/iot-data/model/UpdateThingShadowRequest.h>
#include <aws/iot-data/model/UpdateThingShadowResult.h>
#include <nlohmann/json.hpp>

#include <Panorama/apidefs.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/comptr.h>
#include <Panorama/properties.h>
#include <Panorama/eventing.h>
#include <Panorama/credentials.h>
#include <Panorama/aws.h>

#include <scheduling.h>
#include <misc.h>

#include <core/property/property_manager.h>

using namespace Panorama;
class IoTShadowPropertyDelegate : public UnknownImpl<IPropertyDelegate>
{
public:
    static HRESULT Create(IPropertyDelegate** ppObj, const char* thingName, const char* shadowName, const char* region, ICredentialProvider* credentialProvider)
    {
        HRESULT hr = S_OK;
        CREATE_COM(IoTShadowPropertyDelegate, ptr);
        CHECKHR(ptr->Initialize(thingName, shadowName, region, credentialProvider));
        *ppObj = ptr.Detach();
        return hr;
    }

    ~IoTShadowPropertyDelegate()
    {
        COM_DTOR(IoTShadowPropertyDelegate_v2);
        _running.Set();

        // Forcing this to be destroyed before AwsContext is destroyed
        // If AwsContext goes out of scope first and that was the only reference
        // than Aws SDK will crash when trying to delete _client....0 out of 10!
        _client.reset();
        COM_DTOR_FIN(IoTShadowPropertyDelegate_v2);
    }

    HRESULT GetProperty(IProperty** ppObj, const char* property) override
    {
        return _propertyManager->GetProperty(ppObj, property);
    }

    HRESULT Synchronize(IPropertyCollection** ppObj) override
    {
        HRESULT hr = S_OK;
        CHECKHR(this->GetShadowDocument(ppObj));

        return hr;
    }

private:
    HRESULT Initialize(const char* thingName, const char* shadowName, const char* region, ICredentialProvider* credentialProvider)
    {
        HRESULT hr = S_OK;
        CHECKNULL_MSG(thingName, E_INVALIDARG, "Thing name cannot be null");
        CHECKNULL_MSG(region, E_INVALIDARG, "region cannot be null");
        CHECKNULL_MSG(credentialProvider, E_INVALIDARG, "Credential provider cannot be null");

        CHECKHR(AwsContext(_awsContext.AddressOf()));
        CHECKHR(PropertyManager::Create(_propertyManager.AddressOf(), "IoTShadowPropertyDelegate_v2"));

        _thingName = thingName;
        if(shadowName == nullptr || strlen(shadowName) == 0)
        {
            _classic = true;
        }
        else
        {
            _classic = false;
        }

        _shadowName = shadowName != nullptr ? std::make_shared<Aws::String>(shadowName) : nullptr;

        // Create the iot data plane client
        Aws::Client::ClientConfiguration config;
        config.region = region;

        _client = std::make_shared<Aws::IoTDataPlane::IoTDataPlaneClient>(credentialProvider->CredentialProvider(), config);

        // Get the initial document
        CHECKHR(GetShadowDocument(nullptr));
        return hr;
    }

    HRESULT GetShadowDocument(IPropertyCollection** ppObj)
    {
        HRESULT hr = S_OK;
        TraceVerbose("Fetching document from thing %s:%s", _thingName.c_str(), _classic ? "Classic Shadow" : _shadowName.get()->c_str());

        // Create the request
        Aws::IoTDataPlane::Model::GetThingShadowRequest get_shadow_request;
        get_shadow_request.SetThingName(_thingName);
        if(_classic == false)
        {
            get_shadow_request.SetShadowName(*_shadowName.get());
        }

        // Send request
        Aws::IoTDataPlane::Model::GetThingShadowOutcome outcome = _client->GetThingShadow(get_shadow_request);
        TraceVerbose("Shadow document fetch complete");
        if (outcome.IsSuccess()) 
        {
            // extract the payload
            std::stringstream ss;
            ss << outcome.GetResult().GetPayload().rdbuf();
            std::string payload = ss.str();

            // validate it's json
            if(nlohmann::json::accept(payload) == false)
            {
                TraceError("Received document is not valid json");
                return E_FAIL;
            }

            // Get the desired state
            nlohmann::json jObj = nlohmann::json::parse(payload);
            if(jObj.contains("state") == false || jObj["state"].contains("desired") == false)
            {
                TraceError("Document does not have a desired state");
                return E_FAIL;
            }

            // Set the properties
            CHECKHR(_propertyManager->SetBatchProperty(ppObj, jObj["state"]["desired"]));

            // Upload the reported state
            CHECKHR(UploadReportedState(jObj));
        }
        else 
        {
            TraceError("Error getting shadow document from thing %s: %s", _thingName.c_str(), outcome.GetError().GetMessage().c_str());
            return E_FAIL;
        }

        return hr;
    }

    HRESULT UploadReportedState(nlohmann::json& jObj)
    {
        if(jObj.contains("state") == false)
        {
            return E_FAIL;
        }

        TraceVerbose("Reporting state for thing %s:%s", _thingName.c_str(), _classic ? "Classic Shadow" : _shadowName.get()->c_str());

        // Create the request
        Aws::IoTDataPlane::Model::UpdateThingShadowRequest update_shadow_request;
        update_shadow_request.SetThingName(_thingName);
        if(_classic == false)
        {
            update_shadow_request.SetShadowName(*_shadowName.get());
        }

        // Create the payload
        nlohmann::json newReported = nlohmann::json::parse(_propertyManager->ToJson().c_str());
        if(jObj["state"].contains("reported"))
        {
            NullifyJsonClutter(newReported, jObj["state"]["reported"]);
        }

        nlohmann::json upload;
        upload["state"]["reported"] = std::move(newReported);
        
        // Set the payload
        auto shadow_payload = Aws::MakeShared<Aws::StringStream>("UpdateShadowPayload");
        *shadow_payload << upload.dump();
        shadow_payload->flush();
        update_shadow_request.SetBody(shadow_payload);

        Aws::IoTDataPlane::Model::UpdateThingShadowOutcome outcome = _client->UpdateThingShadow(update_shadow_request);
        if(outcome.IsSuccess() == false)
        {
            TraceError("Error reporting state for thing %s:%s %s", _thingName.c_str(), _classic ? "Classic Shadow" : _shadowName.get()->c_str(), outcome.GetError().GetMessage().c_str());
            return E_FAIL;
        }

        return S_OK;
    }

    Aws::String _thingName;
    std::shared_ptr<Aws::String> _shadowName;
    std::shared_ptr<Aws::IoTDataPlane::IoTDataPlaneClient> _client;
    ComPtr<PropertyManager> _propertyManager;
    std::thread _getDocumentThread;
    ManualResetEvent _running;
    bool _classic = false;

    ComPtr<IAwsContext> _awsContext;
};

DLLAPI HRESULT CreateIoTShadowPropertyDelegate(IPropertyDelegate** ppObj, const char* thingName, const char* shadowName, const char* region, ICredentialProvider* credentialProvider)
{
    return IoTShadowPropertyDelegate::Create(ppObj, thingName, shadowName, region, credentialProvider);
}