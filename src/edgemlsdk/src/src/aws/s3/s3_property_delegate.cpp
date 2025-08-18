#include <iostream>
#include <streambuf>

#include <aws/core/Aws.h>
#include <aws/s3/S3Client.h>
#include <aws/s3/model/GetObjectRequest.h>
#include <aws/s3/model/HeadObjectRequest.h>
#include <aws/core/utils/DateTime.h>
#include <nlohmann/json.hpp>

#include <Panorama/apidefs.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/comptr.h>
#include <Panorama/properties.h>
#include <Panorama/eventing.h>
#include <Panorama/credentials.h>
#include <Panorama/aws.h>

#include <core/property/property_manager.h>
#include <scheduling.h>

using namespace Panorama;
class S3PropertyDelegate : public UnknownImpl<IPropertyDelegate>
{
public:
    static HRESULT Create(IPropertyDelegate** ppObj, const char* bucket, const char* key, const char* region, ICredentialProvider* credentialProvider)
    {
        HRESULT hr = S_OK;
        CREATE_COM(S3PropertyDelegate, ptr);
        CHECKHR(ptr->Initialize(bucket, key, region, credentialProvider));
        *ppObj = ptr.Detach();
        return hr;
    }

    ~S3PropertyDelegate()
    {
        COM_DTOR(S3PropertyDelegate);
        _running.Set();

        // Forcing this to be destroyed before AwsContext is destroyed
        // If AwsContext goes out of scope first and that was the only reference
        // than Aws SDK will crash when trying to delete _client....0 out of 10!
        _client.reset();
        COM_DTOR_FIN(S3PropertyDelegate);
    }

    HRESULT GetProperty(IProperty** ppObj, const char* property) override
    {
        return _propertyManager->GetProperty(ppObj, property);
    }

    HRESULT Synchronize(IPropertyCollection** ppObj) override
    {
        HRESULT hr = S_OK;
        CHECKHR(this->GetS3Artifact(ppObj));
        return hr;
    }

private:
    HRESULT Initialize(const char* bucket, const char* key, const char* region, ICredentialProvider* credentialProvider)
    {
        HRESULT hr = S_OK;
        CHECKNULL_MSG(bucket, E_INVALIDARG, "Bucket name cannot be null");
        CHECKNULL_MSG(key, E_INVALIDARG, "Key name cannot be null");
        CHECKNULL_MSG(region, E_INVALIDARG, "region cannot be null");
        CHECKNULL_MSG(credentialProvider, E_INVALIDARG, "Credential provider cannot be null");

        CHECKHR(AwsContext(_awsContext.AddressOf()));
        CHECKHR(PropertyManager::Create(_propertyManager.AddressOf(), "S3PropertyDelegate"));

        _bucket = bucket;
        _key = key;

        // Create the iot data plane client
        Aws::Client::ClientConfiguration config;
        config.region = region;

        _client = std::make_shared<Aws::S3::S3Client>(credentialProvider->CredentialProvider(), config, Aws::Client::AWSAuthV4Signer::PayloadSigningPolicy::Never, false);
        CHECKNULL(_client.get(), E_OUTOFMEMORY);

        // Get the initial document
        CHECKHR(GetS3Artifact(nullptr));
        return hr;
    }

    HRESULT GetS3Artifact(IPropertyCollection** ppObj)
    {
        HRESULT hr = S_OK;

        // Check when the object was last modified
        // Create a HeadObjectRequest
        Aws::S3::Model::HeadObjectRequest headObjectRequest;
        headObjectRequest.WithBucket(_bucket).WithKey(_key);
        Aws::S3::Model::HeadObjectOutcome headObjectOutcome = _client->HeadObject(headObjectRequest);
        if (headObjectOutcome.IsSuccess() == false)
        {
            TraceError("Could not get last modified time for artifact %s:%s.  %s", _bucket.c_str(), _key.c_str(), headObjectOutcome.GetError().GetMessage().c_str());
            return E_FAIL;
        }

        // Extract and display the last modified time
        Aws::Utils::DateTime lastModifiedTime = headObjectOutcome.GetResult().GetLastModified();
        if(lastModifiedTime <= _lastModified)
        {
            TraceVerbose("S3 bucket has not been modified, skipping artifact download.  %lld %lld", lastModifiedTime.Millis(), _lastModified.Millis());
            CHECKHR(CreatePropertyCollection(ppObj));
            return S_FALSE;
        }

        _lastModified = lastModifiedTime;
        TraceInfo("S3 artifact is newer, last modified at %lld", _lastModified.Millis());

        // Create a GetObjectRequest
        TraceInfo("Downloading s3 artifact %s:%s", _bucket.c_str(), _key.c_str());
        Aws::S3::Model::GetObjectRequest getObjectRequest;
        getObjectRequest.WithBucket(_bucket).WithKey(_key);

        // Get the object and save it to memory
        Aws::S3::Model::GetObjectOutcome getObjectOutcome = _client->GetObject(getObjectRequest);

        std::vector<char> data;
        if (getObjectOutcome.IsSuccess())
        {
            Aws::IOStream& retrievedStream = getObjectOutcome.GetResult().GetBody();
            std::istreambuf_iterator<char> eos;
            std::istreambuf_iterator<char> iit(retrievedStream);
            data.assign(iit, eos);

            std::string doc(data.begin(), data.end());
            if(nlohmann::json::accept(doc) == false)
            {
                TraceError("Contents in artifact %s:%s is not valid json", _bucket.c_str(), _key.c_str());
                return E_FAIL;
            }

            nlohmann::json parse = nlohmann::json::parse(doc);
            CHECKHR(_propertyManager->SetBatchProperty(ppObj, parse));
        }
        else
        {
            TraceError("Error downloading s3 artifact %s:%s.  %s", _bucket.c_str(), _key.c_str(), getObjectOutcome.GetError().GetMessage().c_str());
            return E_FAIL;
        }

        return hr;
    }

    Aws::String _bucket;
    Aws::String _key;
    Aws::Utils::DateTime _lastModified;
    std::shared_ptr<Aws::S3::S3Client> _client;
    ComPtr<PropertyManager> _propertyManager;
    std::thread _getArtifactThread;
    ManualResetEvent _running;

    ComPtr<IAwsContext> _awsContext;
};

DLLAPI HRESULT CreateS3PropertyDelegate(IPropertyDelegate** ppObj, const char* bucket, const char* key, const char* region, ICredentialProvider* credentialProvider)
{
    return S3PropertyDelegate::Create(ppObj, bucket, key, region, credentialProvider);
}