#include <aws/sts/STSClient.h>
#include <aws/sts/model/GetSessionTokenRequest.h>
#include <aws/sts/model/GetSessionTokenResult.h>

#include <Panorama/apidefs.h>
#include <Panorama/credentials.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/comptr.h>

using namespace Panorama;

struct MockPanoramaDeviceCredentialProviderDeleter
{
    void operator()(Aws::Auth::AWSCredentialsProvider*) const 
    { 
        //noop, managed by COM
    }
};

class MockPanoramaDeviceCredentialProvider : public UnknownImpl<ICredentialProvider>
{
public:
    static HRESULT Create(ICredentialProvider** ppObj)
    {
        HRESULT hr = S_OK;
        CREATE_COM(MockPanoramaDeviceCredentialProvider, ptr);
        CHECKHR(ptr->Initialize());
        *ppObj = ptr.Detach();
        return hr;
    }

    std::shared_ptr<Aws::Auth::AWSCredentialsProvider> CredentialProvider() override
    {
        return _credProvider;
    }

    Aws::Auth::AWSCredentials GetAWSCredentials() override
    {
        Aws::STS::STSClient sts_client;
        Aws::STS::Model::GetSessionTokenRequest request;
        request.SetDurationSeconds(3600);

        Aws::STS::Model::GetSessionTokenOutcome outcome = sts_client.GetSessionToken(request);
        if (outcome.IsSuccess())
        {
            const auto& credentials = outcome.GetResult().GetCredentials();
            
            return Aws::Auth::AWSCredentials(
                credentials.GetAccessKeyId().c_str(), 
                credentials.GetSecretAccessKey().c_str(),
                credentials.GetSessionToken().c_str(),
                credentials.GetExpiration()
            );
        }
        else
        {
            TraceError("Error getting session token: %s.  Run aws configure.", outcome.GetError().GetMessage().c_str());
            return Aws::Auth::AWSCredentials();
        }
    }

    HRESULT GetCredentialsAsJSON(IBuffer** ppObj) override
    {
        return E_NOTIMPL;
    }

private:
    HRESULT Initialize()
    {
        _credProvider = std::shared_ptr<Aws::Auth::AWSCredentialsProvider>(this, MockPanoramaDeviceCredentialProviderDeleter());
        return S_OK;
    }

    std::shared_ptr<Aws::Auth::AWSCredentialsProvider> _credProvider;
};

DLLAPI HRESULT CreateMockPanoramaDeviceCredentialProvider(ICredentialProvider** ppObj)
{
    return MockPanoramaDeviceCredentialProvider::Create(ppObj);
}