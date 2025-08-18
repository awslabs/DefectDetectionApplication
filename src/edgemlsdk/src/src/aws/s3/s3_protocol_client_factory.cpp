#include <nlohmann/json.hpp>

#include <Panorama/message_broker.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/aws.h>
#include <misc.h>

using namespace Panorama;

class S3ProtocolFactory : public UnknownImpl<IProtocolFactory>
{
public:
    static constexpr const char* REGION_KEY = "region";
    static constexpr const char* BUCKET_KEY = "bucket";
    static constexpr const char* KEY_KEY = "key";
    static constexpr const char* OVERWRITE_KEY = "overwrite";
    static constexpr const char* BATCH_PAYLOAD_EXPANSION_KEY = "batch_payload_expansion";

    static HRESULT Create(IProtocolFactory** ppObj)
    {
        HRESULT hr = S_OK;
        CREATE_COM(S3ProtocolFactory, ptr);
        *ppObj = ptr.Detach();
        return hr;
    }

    ~S3ProtocolFactory()
    {
        COM_DTOR_FIN(S3ProtocolFactory);
    }

    HRESULT CreateProtocol(IProtocolClient** ppObj, const char* creation_options, ICredentialProvider* credential_provider) override
    {
        HRESULT hr = S_OK;
        CHECKNULL(creation_options, E_INVALIDARG);
        CHECKIF(nlohmann::json::accept(creation_options) == false, E_INVALIDARG);
        nlohmann::json json = nlohmann::json::parse(creation_options);

        CHECKIF_MSG(ValidateJsonProperty<const char *>(json, REGION_KEY, true) == false, E_INVALIDARG, "S3 option '%s' was not defined or is not a string", REGION_KEY);

        std::string region = json[REGION_KEY];

        CHECKHR(Panorama_Aws::S3ProtocolClient(ppObj, region.c_str(), credential_provider));
        TraceVerbose("Created s3 broker from factory");
        return hr;
    }

    HRESULT ValidateMessageOptions(const char* message_options) override
    {
        HRESULT hr = S_OK;

        CHECKNULL(message_options, E_INVALIDARG);
        CHECKIF(nlohmann::json::accept(message_options) == false, E_INVALIDARG);
        nlohmann::json json = nlohmann::json::parse(message_options);

        CHECKIF_MSG(ValidateJsonProperty<const char *>(json, BUCKET_KEY, true) == false, E_INVALIDARG, "Destination targeting s3 did not define '%s' or is not a string", BUCKET_KEY);
        CHECKIF_MSG(ValidateJsonProperty<const char *>(json, KEY_KEY, true) == false, E_INVALIDARG, "Destination targeting s3 did not define '%s' or is not a string", KEY_KEY);
        CHECKIF_MSG(ValidateJsonProperty<bool>(json, OVERWRITE_KEY, false) == false, E_INVALIDARG, "Destination targeting s3 did not define '%s' as a boolean", OVERWRITE_KEY);
        CHECKIF_MSG(ValidateJsonProperty<bool>(json, BATCH_PAYLOAD_EXPANSION_KEY, false) == false, E_INVALIDARG, "Destination targeting s3 did not define '%s' as a boolean", BATCH_PAYLOAD_EXPANSION_KEY);

        return hr;
    }

    HRESULT CreateMessage(IProtocolMessage** ppObj, IPayload* payload, const char* message_options) override
    {
        HRESULT hr = S_OK;
        CHECKNULL(ppObj, E_POINTER);
        CHECKHR(ValidateMessageOptions(message_options));
        nlohmann::json json = nlohmann::json::parse(message_options);

        std::string bucket = json[BUCKET_KEY];
        std::string key = json[KEY_KEY];

        // Default overwrite to true, use value specified in message options only if it exists (optional)
        bool overwrite = json.contains(OVERWRITE_KEY) ? static_cast<bool>(json[OVERWRITE_KEY]) : true;
        // Default to expanding macros for payloads contained within a batch payload, as opposed to expanding the macro for the batch payload itself.
        bool batch_payload_expansion = json.contains(BATCH_PAYLOAD_EXPANSION_KEY) ? static_cast<bool>(json[BATCH_PAYLOAD_EXPANSION_KEY]) : true;

        ComPtr<IS3Message> msg;
        CHECKHR(Panorama_Aws::S3Message(msg.AddressOf(), payload, bucket.c_str(), key.c_str(), overwrite, batch_payload_expansion));

        *ppObj = msg.Detach();
        return hr;
    }

    HRESULT CreateSubscription(IProtocolSubscription** ppObj, const char* subscription_options) override
    {
        // Subscription is not valid for this protocol
        return E_NOTIMPL;
    }

    const char* ProtocolName() override
    {
        return _protocol_name.c_str();
    }

private:
    S3ProtocolFactory() = default;
    std::string _protocol_name = "s3";
};

DLLAPI HRESULT CreateS3ProtocolFactory(IProtocolFactory** ppObj)
{
    return S3ProtocolFactory::Create(ppObj);
}