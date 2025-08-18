#include <string>

#include <gtest/gtest.h>
#include <aws/crt/Api.h>
#include <aws/core/Aws.h>
#include <aws/s3/S3Client.h>
#include <aws/s3/model/PutObjectRequest.h>
#include <nlohmann/json.hpp>

#include <Panorama/properties.h>
#include <Panorama/credentials.h>
#include <Panorama/aws.h>
#include <Panorama/eventing.h>

#include <TestUtils.h>

using namespace Panorama;

HRESULT UpdateTestS3Artifact(std::string content)
{
    HRESULT hr = S_OK;

    ComPtr<ICredentialProvider> creds = Panorama_Aws::DefaultCredentialProvider();
    Aws::Client::ClientConfiguration config;
    config.region = "us-west-2";

    Aws::S3::S3Client client(creds->CredentialProvider(), config, Aws::Client::AWSAuthV4Signer::PayloadSigningPolicy::Never, false);
    Aws::S3::Model::PutObjectRequest putObjectRequest;
    putObjectRequest.WithBucket("panorama-sdk-v2-artifacts").WithKey("testdata/s3propertydelegate/s3PropertyDelegateTest");
    
    auto inputData = Aws::MakeShared<Aws::StringStream>("PutObjectInputStream");
    *inputData << content;
    inputData->flush();
    putObjectRequest.SetBody(inputData);

    auto putObjectOutcome = client.PutObject(putObjectRequest);

    if (putObjectOutcome.IsSuccess() == false)
    {
        TraceError("Could not update test s3 artifact %s", putObjectOutcome.GetError().GetMessage().c_str());
        return E_FAIL;
    }
    
    TraceInfo("Modified s3 delegate test artifact");
    return hr;
}

TEST(AWSTests, S3PropertyDelegateTests)
{
    HRESULT hr = S_OK;

    ComPtr<ICredentialProvider> credProvider = Panorama_Aws::DefaultCredentialProvider();
    
    ComPtr<IPropertyDelegate> s3PropertyDelegate;
    ASSERT_F(CreateS3PropertyDelegate(s3PropertyDelegate.AddressOf(), "panorama-sdk-v2-artifacts", "doesn't exist", "us-west-2", credProvider));
    ASSERT_F(CreateS3PropertyDelegate(s3PropertyDelegate.AddressOf(), "panorama-sdk-v2-artifacts", "testdata/s3propertydelegate/s3PropertyDelegateTest_invalidjson", "us-west-2", credProvider));
    ASSERT_S(CreateS3PropertyDelegate(s3PropertyDelegate.AddressOf(), "panorama-sdk-v2-artifacts", "testdata/s3propertydelegate/s3PropertyDelegateTest", "us-west-2", credProvider));

    ComPtr<IProperty> val1;
    ASSERT_S(s3PropertyDelegate->GetProperty(val1.AddressOf(), "val1"));

    ComPtr<IProperty> val2;
    ASSERT_S(s3PropertyDelegate->GetProperty(val2.AddressOf(), "val2"));

    int32_t val1_init = val1.QueryInterface<IIntegerProperty>()->Get();
    int32_t val2_init = val2.QueryInterface<IIntegerProperty>()->Get();

    // modify the object and validate property change events get invoked
    nlohmann::json json;
    json["val1"] = val1_init + 2;
    json["val2"] = val2_init + 2;
    ASSERT_S(UpdateTestS3Artifact(json.dump()));

    {
        ComPtr<IPropertyCollection> changed_properties;
        ASSERT_S(s3PropertyDelegate->Synchronize(changed_properties.AddressOf()));
        ASSERT_EQ(changed_properties->Count(), 2);
    }

    ASSERT_EQ(val1.QueryInterface<IIntegerProperty>()->Get(), val1_init + 2);
    ASSERT_EQ(val2.QueryInterface<IIntegerProperty>()->Get(), val2_init + 2);

    ThreadSleep(1000);
    json["val1"] = val1_init;
    json["val2"] = val2_init;
    ASSERT_S(UpdateTestS3Artifact(json.dump()));

    {
        ComPtr<IPropertyCollection> changed_properties;
        ASSERT_S(s3PropertyDelegate->Synchronize(changed_properties.AddressOf()));
        ASSERT_EQ(changed_properties->Count(), 2);
    }

    ASSERT_EQ(val1.QueryInterface<IIntegerProperty>()->Get(), val1_init);
    ASSERT_EQ(val2.QueryInterface<IIntegerProperty>()->Get(), val2_init);

    {
        ComPtr<IPropertyCollection> changed_properties;
        ASSERT_S(s3PropertyDelegate->Synchronize(changed_properties.AddressOf()));
        ASSERT_EQ(changed_properties->Count(), 0);
    }
}

TEST(AWSTests, IoTShadowPropertyDelegateTest)
{
    HRESULT hr = S_OK;

    ComPtr<ICredentialProvider> credProvider = Panorama_Aws::DefaultCredentialProvider();
    {
        // Classic Shadow
        ComPtr<IPropertyDelegate> iotShadowDelegate;
        ASSERT_S(CreateIoTShadowPropertyDelegate(iotShadowDelegate.AddressOf(), "PropertiesTest_IoTShadowPropertyDelegateTest", nullptr, "us-west-2", credProvider));
    
        ComPtr<IProperty> val1;
        iotShadowDelegate->GetProperty(val1.AddressOf(), "val1");
        ASSERT_EQ(val1.QueryInterface<IIntegerProperty>()->Get(), 1);

        ComPtr<IProperty> val2;
        iotShadowDelegate->GetProperty(val2.AddressOf(), "val2");
        ASSERT_EQ(val2.QueryInterface<IIntegerProperty>()->Get(), 2);
    }

    {
        // Named Shadow
        ComPtr<IPropertyDelegate> iotShadowDelegate;
        ASSERT_S(CreateIoTShadowPropertyDelegate(iotShadowDelegate.AddressOf(), "PropertiesTest_IoTShadowPropertyDelegateTest", "named_shadow", "us-west-2", credProvider));
    
        ComPtr<IProperty> val1;
        iotShadowDelegate->GetProperty(val1.AddressOf(), "val3");
        ASSERT_EQ(val1.QueryInterface<IIntegerProperty>()->Get(), 3);

        ComPtr<IProperty> val2;
        iotShadowDelegate->GetProperty(val2.AddressOf(), "val4");
        ASSERT_EQ(val2.QueryInterface<IIntegerProperty>()->Get(), 4);
    }
}
