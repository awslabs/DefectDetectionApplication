#include <queue>
#include <nlohmann/json.hpp>
#include <aws/core/Aws.h>
#include <aws/s3/S3Client.h>
#include <aws/s3/model/HeadObjectRequest.h>
#include <aws/s3/model/PutObjectRequest.h>

#include <Panorama/apidefs.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/credentials.h>
#include <Panorama/message_broker.h>
#include <Panorama/eventing.h>
#include <Panorama/aws.h>

#include <misc.h>
#include <scheduling.h>
#include <core/message_broker/protocol_client_base.h>

using namespace Panorama;
using namespace Aws::Crt;
using namespace Aws::Crt::Mqtt;



class S3ProtocolClient : public ProtocolClientBase<IProtocolSubscription>
{
public:
    static HRESULT Create(IProtocolClient** ppObj, const char* region, ICredentialProvider* credential_provider)
    {
        HRESULT hr = S_OK;
        CREATE_COM(S3ProtocolClient, ptr);
        CHECKHR(ptr->Initialize(region, credential_provider));
        *ppObj = ptr.Detach();

        return hr;
    }

    ~S3ProtocolClient()
    {
        COM_DTOR(S3MessageBroker);

        _upload_jobs.Stop();
        _client.reset();
        
        COM_DTOR_FIN(S3MessageBroker);
    }

    HRESULT Publish(IProtocolMessage* message) override
    {
        HRESULT hr = S_OK;
        CHECKNULL(message, E_INVALIDARG);
        ComPtr<IS3Message> msg = ComPtr<IProtocolMessage>(message).QueryInterface<IS3Message>();
        CHECKNULL(msg, E_NOINTERFACE);
        CHECKHR(ProcessPayload(msg->GetPayload(), msg->Bucket(), msg->Key(), msg->Overwrite(), msg->BatchPayloadExpansion()));
        return hr;
    }

    HRESULT PublishAsync(IProtocolMessage* message, IProtocolClientEventHandler* eventHandler) override
    {
        HRESULT hr = S_OK;
        CHECKNULL(message, E_INVALIDARG);
        ComPtr<IS3Message> msg = ComPtr<IProtocolMessage>(message).QueryInterface<IS3Message>();
        CHECKNULL(msg, E_NOINTERFACE);

        _upload_jobs.Enqueue(msg, [
            msg, 
            friendly_name = _friendly_name,
            handler = ComPtr<IProtocolClientEventHandler>(eventHandler)](HRESULT hr)
            {
                if(handler)
                {
                    handler->OnMessagePublished(friendly_name.c_str(), msg, SUCCEEDED(hr));
                }
            });


        return hr;
    }

    HRESULT Reconnect() override 
    {
        return S_OK;
    }

    const char* FriendlyName() override
    {
        return _friendly_name.c_str();
    }

protected:
    HRESULT OnSubscription(IProtocolSubscription* subscription) override
    {
        return E_NOTIMPL;
    }

    HRESULT OnUnsubscribe(IProtocolSubscription* subscription) override
    {
        return E_NOTIMPL;
    }

private:
    HRESULT Initialize(const char* region, ICredentialProvider* credential_provider)
    {
        HRESULT hr = S_OK;
        CHECKNULL(region, E_INVALIDARG);
        CHECKNULL(credential_provider, E_INVALIDARG);
        CHECKHR(AwsContext(_aws_context.AddressOf()));

        _region = region;
        _credential_provider = credential_provider;

        Aws::Client::ClientConfiguration config;
        config.region = _region;
        _client = std::make_shared<Aws::S3::S3Client>(_credential_provider->CredentialProvider(), config, Aws::Client::AWSAuthV4Signer::PayloadSigningPolicy::Never, false);
        CHECKNULL(_client.get(), E_OUTOFMEMORY);
        
        _upload_jobs.SetProcessor([&](ComPtr<IS3Message> msg)
        {
            CHECKNULL(msg, E_INVALIDARG);
            return this->ProcessPayload(msg->GetPayload(), msg->Bucket(), msg->Key(), msg->Overwrite(), msg->BatchPayloadExpansion());
        });
        _upload_jobs.Start();
        return hr;
    }

    HRESULT ProcessPayload(IPayload* payload, const char* bucket, const char* key, bool overwrite, bool batchPayloadExpansion)
    {
        HRESULT hr = S_OK;
        CHECKNULL(payload, E_INVALIDARG);
        CHECKNULL_OR_EMPTY(bucket, E_INVALIDARG);
        CHECKNULL_OR_EMPTY(key, E_INVALIDARG);
        ComPtr<IBatchPayload> batchPayload = ComPtr<IPayload>(payload).QueryInterface<IBatchPayload>();
        if (batchPayload != nullptr)
        {
            if (batchPayloadExpansion)
            {
                for (int i = 0; i < batchPayload->Count(); i++)
                {
                    ComPtr<IPayload> subPayload;
                    CHECKHR(batchPayload->Payload(subPayload.AddressOf(), i));
                    CHECKHR(ProcessPayload(subPayload, bucket, key, overwrite, batchPayloadExpansion));
                }
            }
            else
            {
                // Expand macros for the batch payload itself, then upload its manifest to the specified bucket/key.
                std::string expandedKey = ExpandMacros(key, batchPayload);
                CHECKIF_MSG(expandedKey.length() == 0, E_INVALIDARG, "Failed to expand macro for key");
                std::string expandedBucket = ExpandMacros(bucket, batchPayload);
                CHECKIF_MSG(expandedBucket.length() == 0, E_INVALIDARG, "Failed to expand macro for bucket");
                CHECKHR(Upload(batchPayload, expandedBucket.c_str(), expandedKey.c_str(), overwrite));
            }
        }
        else
        {
            std::string expandedKey = ExpandMacros(key, payload);
            CHECKIF_MSG(expandedKey.length() == 0, E_INVALIDARG, "Failed to expand macro for key");
            std::string expandedBucket = ExpandMacros(bucket, payload);
            CHECKIF_MSG(expandedBucket.length() == 0, E_INVALIDARG, "Failed to expand macro for bucket");
            CHECKHR(Upload(payload, expandedBucket.c_str(), expandedKey.c_str(), overwrite));
        }
        return hr;
    }

    HRESULT Upload(IPayload* payload, const char* bucket, const char* key, bool overwrite)
    {
        HRESULT hr = S_OK;
        CHECKNULL(payload, E_INVALIDARG);
        CHECKNULL_OR_EMPTY(bucket, E_INVALIDARG);
        CHECKNULL_OR_EMPTY(key, E_INVALIDARG);
        
        // If don't want to overwrite, return early if the key already exists
        if (!overwrite)
        {
            Aws::S3::Model::HeadObjectRequest request;
            request.WithBucket(bucket).WithKey(key);
            const auto response = _client->HeadObject(request);
            if (response.IsSuccess())
            {
                TraceDebug("Skipping upload of bucket '%s' and key '%s' as it already exists and requested no overwrites", bucket, key);
                return hr;
            }
        }

        ComPtr<IBuffer> serialized;
        CHECKHR(payload->Serialize(serialized.AddressOf()));

        // Create and configure the put request
        Aws::S3::Model::PutObjectRequest putRequest;
        putRequest.WithBucket(bucket).WithKey(key);

        auto inputData = Aws::MakeShared<Aws::StringStream>("PutObjectInputStream");
        inputData->write(reinterpret_cast<const char*>(serialized->Data()), serialized->Size());
        putRequest.SetBody(inputData);

        // Put the object into the S3 bucket
        const auto putOutcome = _client->PutObject(putRequest);

        if (putOutcome.IsSuccess() == false) 
        {
            TraceError("Error while uploading to s3: %s", putOutcome.GetError().GetMessage().c_str());
            return E_FAIL;
        } 

        return hr;
    }

    std::string _region;
    std::shared_ptr<Aws::S3::S3Client> _client;
    ComPtr<ICredentialProvider> _credential_provider;
    JobQueue<ComPtr<IS3Message>> _upload_jobs;
    ComPtr<IAwsContext> _aws_context;
    std::string _friendly_name = "s3";
};

DLLAPI HRESULT CreateS3ProtocolClient(IProtocolClient** ppObj, const char* region, ICredentialProvider* credential_provider)
{
    return S3ProtocolClient::Create(ppObj, region, credential_provider);
}