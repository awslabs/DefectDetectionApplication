#include <thread>
#include <aws/secretsmanager/SecretsManagerClient.h>
#include <aws/secretsmanager/model/GetSecretValueRequest.h>
#include <aws/secretsmanager/model/DescribeSecretRequest.h>
#include <aws/core/utils/ARN.h>

#include <Panorama/comptr.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/eventing.h>
#include <Panorama/aws.h>
#include <Panorama/properties.h>
#include <Panorama/buffer.h>
#include <Panorama/credentials.h>
#include <core/property/expansion_base.h>
#include <misc.h>

using namespace Panorama;

HRESULT GetSecretsId(std::string* arn, std::string* key, const char* val)
{
    std::vector<std::string> split = SplitString(val, ';');
    CHECKIF_MSG(split.size() != 2, E_FAIL, "Value isn't in form of <arn>;<key>");
    
    *arn = split[0];
    *key = split[1];

    return S_OK;
}

std::shared_ptr<Aws::SecretsManager::SecretsManagerClient> CreateSMClient(const char* arnString, ICredentialProvider* cred_provider)
{
    HRESULT hr = S_OK;
    CHECKNULL(arnString, nullptr);

    // Use AWS Apis to fetch secret
    Aws::Client::ClientConfiguration config;

    // Get the region from the arn so it can specified on the config
    Aws::Utils::ARN arn(arnString);
    config.region = arn.GetRegion();

    // Create the secrets manager client
    std::shared_ptr<Aws::SecretsManager::SecretsManagerClient> sm_client;
    sm_client = std::make_shared<Aws::SecretsManager::SecretsManagerClient>(cred_provider->CredentialProvider(), config);

    return sm_client;
}

HRESULT GetSecretsManagerValue(IBuffer** ppObj, const char* arn, const char* key, ICredentialProvider* cred_provider)
{
    HRESULT hr = S_OK;
    TraceVerbose("Fetching secret at %s;%s", arn, key);

    std::shared_ptr<Aws::SecretsManager::SecretsManagerClient> sm_client = CreateSMClient(arn, cred_provider);
    CHECKNULL(sm_client, E_FAIL);

    Aws::SecretsManager::Model::GetSecretValueRequest request;
    request.SetSecretId(arn);

    // Query the secrets manager for the json blobs
    Aws::SecretsManager::Model::GetSecretValueOutcome result = sm_client->GetSecretValue(request);

    if(result.IsSuccess() == false)
    {
        TraceError("Aws::SecretsManager::Model::GetSecretValueOutcome does not indicate success: %s", result.GetError().GetMessage().c_str());
        return E_FAIL;
    }

    TraceVerbose("Successfully fetched secret from secretsmanager");
    std::string json = result.GetResult().GetSecretString();
    CHECKIF_MSG(nlohmann::json::accept(json.c_str()) == false, E_INVALIDARG, "Could not parse json");
    nlohmann::json obj = nlohmann::json::parse(json.c_str());
    CHECKIF_MSG(obj.contains(key) == false, E_INVALIDARG, "Json did not contain specified key");

    return CreateBufferFromString(ppObj, std::string(obj[key]).c_str());
}

HRESULT GetLastModifiedTime(Aws::Utils::DateTime* last_changed, const char* arn, ICredentialProvider* cred_provider)
{
    HRESULT hr = S_OK;
    CHECKNULL(last_changed, E_POINTER);

    std::shared_ptr<Aws::SecretsManager::SecretsManagerClient> sm_client = CreateSMClient(arn, cred_provider);
    CHECKNULL(sm_client, E_FAIL);

    Aws::SecretsManager::Model::DescribeSecretRequest request;
    request.SetSecretId(arn);

    Aws::SecretsManager::Model::DescribeSecretOutcome result = sm_client->DescribeSecret(request);
    CHECKIF_MSG(result.IsSuccess() == false, E_FAIL, "Aws::SecretsManager::Model::DescribeSecretRequest does not indicate success: %s", result.GetError().GetMessage().c_str());

    *last_changed = result.GetResult().GetLastChangedDate();
    return hr;
}

class SecretsManagerExpansion : public ExpansionBase<std::string>
{
public:
    static HRESULT Create(IVariableExpansion** ppObj, IStringProperty* property, ICredentialProvider* credential_provider)
    {
        COM_FACTORY(SecretsManagerExpansion, Initialize(property, credential_provider));
    }

    ~SecretsManagerExpansion()
    {
        COM_DTOR_FIN(SecretsManagerExpansion);
    }

    HRESULT Expand(IBuffer** ppObj) override
    {
        HRESULT hr = S_OK;
        CHECKNULL(ppObj, E_POINTER);
        CHECKHR(Parse());

        CHECKHR(GetSecretsManagerValue(_secretsValue.AddressOf(), _arn.c_str(), _key.c_str(), _cred_provider));
        CHECKHR(GetLastModifiedTime(&_last_changed, _arn.c_str(), _cred_provider));

        *ppObj = _secretsValue.Detach();
        return hr;
    }

protected:
    bool IsStale() override
    {
        // Check if the SM object has been changed since last call to Expand
        HRESULT hr = S_OK;
        Aws::Utils::DateTime last_changed;
        CHECK_FAIL(GetLastModifiedTime(&last_changed, _arn.c_str(), _cred_provider), false);
        return _last_changed < last_changed;
    }

    HRESULT ParseValue(const std::string& value) override
    {
        HRESULT hr = S_OK;
        CHECKHR(GetSecretsId(&_arn, &_key, value.c_str()));
        return hr;
    }

private:
    HRESULT Initialize(IStringProperty* property, ICredentialProvider* credential_provider)
    {
        HRESULT hr = S_OK;
        CHECKHR(InitializeBase(property));
        CHECKNULL(credential_provider, E_INVALIDARG);
        CHECKHR(AwsContext(_aws_context.AddressOf()));

        _cred_provider = credential_provider;

        CHECKHR(Parse());
        return hr;
    }

    Aws::Utils::DateTime _last_changed;
    ComPtr<ICredentialProvider> _cred_provider;
    ComPtr<IAwsContext> _aws_context;
    ComPtr<IBuffer> _secretsValue;
    std::string _arn;
    std::string _key;
    SecretsManagerExpansion() = default;
};

DLLAPI HRESULT CreateSecretsManagerExpansion(IVariableExpansion** ppObj, IStringProperty* property, ICredentialProvider* credential_provider)
{
    return SecretsManagerExpansion::Create(ppObj, property, credential_provider);
}