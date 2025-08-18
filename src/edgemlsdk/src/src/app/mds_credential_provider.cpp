#include <nlohmann/json.hpp>

#include <Panorama/comptr.h>
#include <Panorama/app.h>
#include <Panorama/flowcontrol.h>

#include <core/rest/rest.h>

using namespace Panorama;

#define USER_AGENT "Node Runtime"

struct MDSCredentialProviderDeleter
{
    void operator()(Aws::Auth::AWSCredentialsProvider*) const 
    { 
        //noop, managed by COM
    }
};

class MDS_CredentialProvider : public UnknownImpl<ICredentialProvider>
{
public:
    static HRESULT Create(ICredentialProvider** ppObj, IMDSClient* mds_client)
    {
        HRESULT hr = S_OK;
        CREATE_COM(MDS_CredentialProvider, ptr);
        CHECKHR(ptr->Initialize(mds_client));
        *ppObj = ptr.Detach();
        return hr;
    }

    ~MDS_CredentialProvider()
    {
        COM_DTOR_FIN(MDS_CredentialProvider);
    }

    Aws::Auth::AWSCredentials GetAWSCredentials() override
    {
        HRESULT hr = S_OK;

        if(_cachedCredentials.IsExpiredOrEmpty())
        {
            TraceVerbose("Cached credentials are %s, refreshing", _cachedCredentials.IsEmpty() ? "empty" : "expired");
            ComPtr<IBuffer> credentials;
            CHECK_FAIL(_mds_client->GetCredentials(credentials.AddressOf()), Aws::Auth::AWSCredentials());
            _cachedCredentials = ParseCredentials(credentials->AsString());
        }

        return _cachedCredentials;
    }

    std::shared_ptr<Aws::Auth::AWSCredentialsProvider> CredentialProvider() override
    {
        return _credProvider;
    }

    HRESULT GetCredentialsAsJSON(IBuffer** ppObj) override
    {
        HRESULT hr = S_OK;
        CHECKNULL(ppObj, E_POINTER);
        
        Aws::Auth::AWSCredentials creds = this->CredentialProvider()->GetAWSCredentials();
        nlohmann::json jObj;

        jObj["access_key"] = creds.GetAWSAccessKeyId().c_str();
        jObj["secret_key"] = creds.GetAWSSecretKey().c_str();
        jObj["token"] = creds.GetSessionToken().c_str();
        jObj["expiry_time"] = creds.GetExpiration().ToGmtString(Aws::Utils::DateFormat::ISO_8601_BASIC).c_str();

        ComPtr<IBuffer> buffer;
        CHECKHR(CreateBufferFromString(buffer.AddressOf(), jObj.dump().c_str()));
        *ppObj = buffer.Detach();

        return hr;
    }

private:
    MDS_CredentialProvider() = default;

    HRESULT Initialize(IMDSClient* mds_client)
    {
        HRESULT hr = S_OK;
        CHECKNULL(mds_client, E_INVALIDARG);
        TraceVerbose("Initializing MDS Credential Provider");
        _mds_client = mds_client;
        _credProvider = std::shared_ptr<Aws::Auth::AWSCredentialsProvider>(this, MDSCredentialProviderDeleter());
        return hr;
    }

    Aws::Auth::AWSCredentials ParseCredentials(std::string credentials)
    {
        static std::vector<std::string> accessKey_Keys = { "accessKeyId", "AccessKeyId" };
        static std::vector<std::string> secret_Keys = { "secretAccessKey", "SecretAccessKey" };
        static std::vector<std::string> token_Keys = { "sessionToken", "SessionToken", "Token"};
        static std::vector<std::string> expiration_Keys = { "expiration", "Expiration" };

        Aws::Auth::AWSCredentials creds;

        if(nlohmann::json::accept(credentials) == false)
        {
            TraceError("Received non json response from MDS when requesting credentials");
            return creds;
        }
        
        nlohmann::json json = nlohmann::json::parse(credentials);
        for (int32_t idx = 0; idx < accessKey_Keys.size(); idx++)
        {
            if (json.contains(accessKey_Keys[idx]))
            {
                creds.SetAWSAccessKeyId(json[accessKey_Keys[idx]]);
                break;
            }
        }

        for (int32_t idx = 0; idx < secret_Keys.size(); idx++)
        {
            if (json.contains(secret_Keys[idx]))
            {
                creds.SetAWSSecretKey(json[secret_Keys[idx]]);
                break;
            }
        }

        for (int32_t idx = 0; idx < token_Keys.size(); idx++)
        {
            if (json.contains(token_Keys[idx]))
            {
                creds.SetSessionToken(json[token_Keys[idx]]);
                break;
            }
        }

        for (int32_t idx = 0; idx < expiration_Keys.size(); idx++)
        {
            if (json.contains(expiration_Keys[idx]))
            {
                nlohmann::json exp = json[expiration_Keys[idx]];
                if (exp.is_string())
                {
                    std::string expString = exp;

                    // The +0000 returned from MDS is not parsable by Aws::Utits::DateTime
                    // Need to replace it with a 'Z'
                    int32_t idx = expString.find("+0000");
                    if (idx >= 0)
                    {
                        expString.replace(idx, 1, "Z");
                        expString.resize(expString.size() - 4);
                    }

                    creds.SetExpiration(Aws::Utils::DateTime(expString, Aws::Utils::DateFormat::AutoDetect));
                }
                else
                {
                    creds.SetExpiration(static_cast<int64_t>(exp));
                }

                break;
            }
        }

        return creds;
    }

    ComPtr<IMDSClient> _mds_client;
    std::shared_ptr<Aws::Auth::AWSCredentialsProvider> _credProvider;
    Aws::Auth::AWSCredentials _cachedCredentials;
};

DLLAPI HRESULT CreateMDSCredentialProvider(ICredentialProvider** ppObj, IMDSClient* client)
{
    return MDS_CredentialProvider::Create(ppObj, client);
}