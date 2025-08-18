#include <nlohmann/json.hpp>

#include <Panorama/credentials.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/comptr.h>

using namespace Panorama;

struct DefaultCredentialProviderDeleter
{
    void operator()(Aws::Auth::AWSCredentialsProvider*) const 
    { 
        //noop, managed by COM
    }
};

class DefaultCredentialProvider : public UnknownImpl<ICredentialProvider>
{
public:
    static HRESULT Create(ICredentialProvider** ppObj)
    {
        HRESULT hr = S_OK;
        CREATE_COM(DefaultCredentialProvider, ptr);
        *ppObj = ptr.Detach();
        return hr;
    }

    ~DefaultCredentialProvider()
    {
        COM_DTOR_FIN(DefaultCredentialProvider);
    }

    Aws::Auth::AWSCredentials GetAWSCredentials() override
    {
        return Aws::MakeShared<Aws::Auth::EnvironmentAWSCredentialsProvider>("EnvCredentials")->GetAWSCredentials();
    }

    std::shared_ptr<Aws::Auth::AWSCredentialsProvider> CredentialProvider() override
    {
        return Aws::MakeShared<Aws::Auth::EnvironmentAWSCredentialsProvider>("EnvCredentials");
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
    DefaultCredentialProvider() = default;
};

DLLAPI HRESULT CreateDefaultAwsCredentialProvider(ICredentialProvider** ppObj)
{
    return DefaultCredentialProvider::Create(ppObj);
}