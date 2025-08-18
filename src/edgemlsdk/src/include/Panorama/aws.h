#ifndef __AWS_H__
#define __AWS_H__

#include <Panorama/credentials.h>
#include <Panorama/comptr.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/properties.h>
#include <Panorama/message_broker.h>

namespace Panorama
{
    DEF_INTERFACE(IAwsContext, "{DFD58835-4019-46CA-9D7C-AEF1F3CDA1C2}", IUnknownAlias)
    {
    };

    DEF_INTERFACE(IMqttMessage, "{D027A84C-B7AC-4BEF-B5ED-B519ACEC3EA0}", IProtocolMessage)
    {
        /// @brief The mqtt topic on which to publish the message
        virtual const char* Topic() = 0;
    };

    DEF_INTERFACE(IMqttSubscription, "{6A1C3B25-F729-4E49-8426-5C6E75F0B91D}", IProtocolSubscription)
    {
        /// @brief The mqtt topic on which to listen for messages
        virtual const char* Topic() = 0;
    };

    DEF_INTERFACE(IS3Message, "{4D2C339F-D34D-4BD6-9A16-D324F64E1B89}", IProtocolMessage)
    {
        virtual const char* Bucket() = 0;
        virtual const char* Key() = 0;
        virtual bool Overwrite() = 0;
        virtual bool BatchPayloadExpansion() = 0;
    };

    DLLAPI HRESULT CreateDefaultAwsCredentialProvider(ICredentialProvider** ppObj);
    DLLAPI HRESULT CreateS3PropertyDelegate(IPropertyDelegate** ppObj, const char* bucket, const char* key, const char* region, ICredentialProvider* credentialProvider);
    DLLAPI HRESULT CreateIoTShadowPropertyDelegate(IPropertyDelegate** ppObj, const char* thingName, const char* shadowName, const char* region, ICredentialProvider* credentialProvider);
    DLLAPI HRESULT CreateSecretsManagerExpansion(IVariableExpansion** ppObj, IStringProperty* property, ICredentialProvider* credential_provider);
    DLLAPI HRESULT CreateS3Expansion(IVariableExpansion** ppObj, IStringProperty* property, ICredentialProvider* credential_provider);
    DLLAPI HRESULT AwsContext(IAwsContext** ppObj);
    
    DLLAPI HRESULT CreateMQTTProtocolClient(IProtocolClient** ppObj, const char* endpoint, const char* region, ICredentialProvider* credential_provider, const char* client_id);
    DLLAPI HRESULT CreateMqttMessage(IMqttMessage** ppObj, IPayload* payload, const char* topic);
    DLLAPI HRESULT CreateMqttSubscription(IMqttSubscription** ppObj, const char* topic);
    DLLAPI HRESULT CreateMqttProtocolFactory(IProtocolFactory** ppObj);

    DLLAPI HRESULT CreateS3ProtocolClient(IProtocolClient** ppObj, const char* region, ICredentialProvider* credential_provider);
    DLLAPI HRESULT CreateS3Message(IS3Message** ppObj, IPayload* payload, const char* bucket, const char* key, bool overwrite = true, bool batch_payload_expansion= true);
    DLLAPI HRESULT CreateS3ProtocolFactory(IProtocolFactory** ppObj);
    
    class Panorama_Aws
    {
    public:
        static ComPtr<ICredentialProvider> DefaultCredentialProvider()
        {
            HRESULT hr;
            ComPtr<ICredentialProvider> ptr;
            CHECK_FAIL(CreateDefaultAwsCredentialProvider(ptr.AddressOf()), nullptr);
            return ptr;
        }

        /// @brief Creates property delegate that pulls from s3
        /// @param ppObj Address of the object that will points to the allocated object
        /// @param bucket S3 Bucket
        /// @param key Artifact key
        /// @param region AWS Region
        /// @param credentialProvider Object that will provide AWS credentials
        /// @return Relevant Error Code
        static ComPtr<IPropertyDelegate> S3PropertyDelegate(const char* bucket, const char* key, const char* region, ICredentialProvider* credentialProvider)
        {
            HRESULT hr;
            ComPtr<IPropertyDelegate> ptr;
            CHECK_FAIL(Panorama::CreateS3PropertyDelegate(ptr.AddressOf(), bucket, key, region, credentialProvider), nullptr);
            return ptr;
        }

        /// @brief Creates the IoT Shadow Property delegate that will fetch properties for a shadow document
        /// @param ppObj Address of IPropertyDelegate* that will point to the created IPropertyDelegate
        /// @param thingName Name of your IoT Thing
        /// @param shadowName Name of your IoT Shadow, nullptr indicates that the Classic Shadow will be used 
        /// @param region AWS region
        /// @param credentialProvider Object that will provide AWS credentials
        /// @return Error Code
        static ComPtr<IPropertyDelegate> IotShadowPropertyDelegate(const char* thingName, const char* shadowName, const char* region, ICredentialProvider* credentialProvider)
        {
            HRESULT hr;
            ComPtr<IPropertyDelegate> ptr;
            CHECK_FAIL(Panorama::CreateIoTShadowPropertyDelegate(ptr.AddressOf(), thingName, shadowName, region, credentialProvider), nullptr);
            return ptr;
        }

        /// @brief Gets a handle to the initialize API context.  Once all references to the handle have been released then Aws::ShutdownAPI() will be called
        static ComPtr<IAwsContext> AwsContext()
        {
            HRESULT hr;
            ComPtr<IAwsContext> ptr;
            CHECK_FAIL(Panorama::AwsContext(ptr.AddressOf()), nullptr);
            return ptr;
        }

        /// @brief Creates a message broker that is inteded to communicate over MQTT
        static HRESULT MqttProtocolClient(IProtocolClient** ppObj, const char* endpoint, const char* region, ICredentialProvider* credential_provider, const char* client_id=nullptr)
        {
            return CreateMQTTProtocolClient(ppObj, endpoint, region, credential_provider, client_id);
        }

        /// @brief Create a Mqtt Message from a string
        /// @param ppObj Pointer to the created object
        /// @param contents The contents to create a payload from
        /// @param topic The topic to publish too
        /// @return S_OK on success.  Error Code on failure.
        static HRESULT MqttMessage(IMqttMessage** ppObj, const char* contents, const char* topic)
        {
            HRESULT hr = S_OK;
            ComPtr<IPayload> payload;
            CHECKHR(MessageBroker::CreatePayload(payload.AddressOf(), contents));
            CHECKHR(CreateMqttMessage(ppObj, payload, topic));
            return hr;
        }

        /// @brief Creates a Mqtt Message from a IPayload
        /// @param ppObj Pointer to the created object
        /// @param payload The payload of the message
        /// @param topic The topic to publish too
        /// @return S_OK on success.  Error Code on failure.
        static HRESULT MqttMessage(IMqttMessage** ppObj, IPayload* payload, const char* topic)
        {
            return CreateMqttMessage(ppObj, payload, topic);
        }

        /// @brief Creates the IMqttSubscription implementation of a IProtocolClientSubscription
        /// @param ppObj Pointer to the created object
        /// @param topic The topic to subscribe too
        /// @return S_OK on success.  Error code on failure
        static HRESULT MqttSubscription(IMqttSubscription** ppObj, const char* topic)
        {
            return CreateMqttSubscription(ppObj, topic);
        }

        /// @brief Creates a message broker that is inteded to communicate over S3.  This is only useful for publishing.  No subscribe functionality
        /// @param Pointer to the created object
        /// @param region the aws region
        /// @param credential_provider Object that provides credentials to the S3 client
        static HRESULT S3ProtocolClient(IProtocolClient** ppObj, const char* region, ICredentialProvider* credential_provider)
        {
            return CreateS3ProtocolClient(ppObj, region, credential_provider);
        }

        /// @brief Create a S3 Message from a string
        /// @param ppObj Pointer to the created objected
        /// @param contents The contents to create a payload from
        /// @param bucket Bucket to store the data
        /// @param key Key to store the data
        /// @param overwrite Whether or not to overwrite the existing contents of the bucket/key, if it exists. Optional, defaults to true
        /// @param batch_payload_expansion Whether or not to upload and expand macros for payloads contained within a batch payload, as opposed to the batch payload itself. Optional, defaults to true
        /// @return S_OK on success.  Error Code on failure.
        static HRESULT S3Message(IS3Message** ppObj, const char* contents, const char* bucket, const char* key, bool overwrite = true, bool batch_payload_expansion = true)
        {
            HRESULT hr = S_OK;
            ComPtr<IPayload> payload;
            CHECKHR(MessageBroker::CreatePayload(payload.AddressOf(), contents));
            return CreateS3Message(ppObj, payload, bucket, key, overwrite, batch_payload_expansion);
        }

        /// @brief Create a S3 Message from a IPayload
        /// @param ppObj Pointer to the created objected
        /// @param payload The payload of the message
        /// @param bucket Bucket to store the data
        /// @param key Key to store the data
        /// @param overwrite Whether or not to overwrite the existing contents of the bucket/key, if it exists. Optional, defaults to true
        /// @param batch_payload_expansion Whether or not to upload and expand macros for payloads contained within a batch payload, as opposed to the batch payload itself. Optional, defaults to true
        /// @return S_OK on success.  Error Code on failure.
        static HRESULT S3Message(IS3Message** ppObj, IPayload* payload, const char* bucket, const char* key, bool overwrite = true, bool batch_payload_expansion = true)
        {
            return CreateS3Message(ppObj, payload, bucket, key, overwrite, batch_payload_expansion);
        }
    };
}

#endif