#include <nlohmann/json.hpp>

#include <Panorama/aws.h>
#include <Panorama/message_broker.h>
#include <Panorama/flowcontrol.h>
#include <misc.h>

using namespace Panorama;

class MqttProtocolFactory : public UnknownImpl<IProtocolFactory>
{
public:
    static HRESULT Create(IProtocolFactory** ppObj)
    {
        HRESULT hr = S_OK;
        CREATE_COM(MqttProtocolFactory, ptr);
        *ppObj = ptr.Detach();
        return hr;
    }

    ~MqttProtocolFactory()
    {
        COM_DTOR_FIN(MqttTargetFactory);
    }

    HRESULT CreateProtocol(IProtocolClient** ppObj, const char* creation_options, ICredentialProvider* credential_provider) override
    {
        HRESULT hr = S_OK;
        CHECKNULL(creation_options, E_INVALIDARG);
        CHECKIF(nlohmann::json::accept(creation_options) == false, E_INVALIDARG);
        nlohmann::json json = nlohmann::json::parse(creation_options);

        CHECKIF_MSG(ValidateJsonProperty<const char *>(json, "endpoint") == false, E_INVALIDARG, "Mqtt option 'endpoint' was not defined or is not a string");
        CHECKIF_MSG(ValidateJsonProperty<const char *>(json, "region") == false, E_INVALIDARG, "Mqtt option 'region' was not defined or is not a string");
        CHECKIF_MSG(ValidateJsonProperty<const char *>(json, "client-id", false) == false, E_INVALIDARG, "Mqtt option 'client-id' was provided but it is not a string");

        std::string endpoint = json["endpoint"];
        std::string region = json["region"];
        std::string client_id;
        if(json.contains("client-id"))
        {
            client_id = json["client-id"];
        }
        
        CHECKHR(Panorama_Aws::MqttProtocolClient(ppObj, endpoint.c_str(), region.c_str(), credential_provider, client_id.c_str()));
        TraceVerbose("Created mqtt broker from factory");
        return hr;
    }

    HRESULT ValidateMessageOptions(const char* message_options) override
    {
        HRESULT hr = S_OK;

        CHECKNULL(message_options, E_INVALIDARG);
        CHECKIF(nlohmann::json::accept(message_options) == false, E_INVALIDARG);
        nlohmann::json json = nlohmann::json::parse(message_options);

        CHECKIF_MSG(ValidateJsonProperty<const char *>(json, "topic", true) == false, E_INVALIDARG, "Destination targetting mqtt did not define 'topic' or is not a string");

        return hr;
    }

    HRESULT CreateMessage(IProtocolMessage** ppObj, IPayload* payload, const char* message_options) override
    {
        HRESULT hr = S_OK;
        CHECKNULL(ppObj, E_POINTER);
        CHECKHR(ValidateMessageOptions(message_options));
        nlohmann::json json = nlohmann::json::parse(message_options);

        std::string topic = json["topic"];

        ComPtr<IMqttMessage> msg;
        Panorama_Aws::MqttMessage(msg.AddressOf(), payload, topic.c_str());

        *ppObj = msg.Detach();
        return hr;
    }

    HRESULT CreateSubscription(IProtocolSubscription** ppObj, const char* subscription_options) override
    {
        HRESULT hr = S_OK;
        CHECKNULL(ppObj, E_POINTER);
        CHECKNULL_OR_EMPTY(subscription_options, E_INVALIDARG);
        CHECKIF(nlohmann::json::accept(subscription_options) == false, E_INVALIDARG);
        nlohmann::json json = nlohmann::json::parse(subscription_options);

        CHECKIF_MSG(ValidateJsonProperty<const char *>(json, "topic", true) == false, E_INVALIDARG, "Subscription for mqtt did not define 'topic' or is not a string");
        std::string topic = json["topic"];

        ComPtr<IMqttSubscription> subscription;
        CHECKHR(Panorama_Aws::MqttSubscription(subscription.AddressOf(), topic.c_str()));
        *ppObj = subscription.Detach();
        return hr;
    }

    const char* ProtocolName() override
    {
        return _protocol_name.c_str();
    }

private:
    MqttProtocolFactory() = default;
    std::string _protocol_name = "mqtt";
};

DLLAPI HRESULT CreateMqttProtocolFactory(IProtocolFactory** ppObj)
{
    return MqttProtocolFactory::Create(ppObj);
}