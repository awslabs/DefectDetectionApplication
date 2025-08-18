// // standard headers
// #include <thread>

// depednencies headers
#include <gtest/gtest.h>
#include <aws/core/Aws.h>
#include <aws/s3/S3Client.h>
#include <aws/s3/model/PutObjectRequest.h>
#include <aws/s3/model/GetObjectRequest.h>
#include <aws/s3/model/HeadObjectRequest.h>
#include <aws/core/utils/DateTime.h>
#include <nlohmann/json.hpp>

// Panorama public headers
#include <Panorama/comobj.h>
#include <Panorama/comptr.h>
#include <Panorama/eventing.h>
#include <Panorama/app.h>
#include <Panorama/aws.h>

// Panorama private headers
#include <tools/mockdevice/mock_panorama_device.h>
#include <env_vars.h>

// local headers
#include <TestUtils.h>
#include <test_app_request_handler.h>

using namespace std;
using namespace Panorama;

TEST(PanoramaAppTests, AwsCredentialProvider)
{
    ComPtr<IAwsContext> context = Panorama_Aws::AwsContext();

    // Test that the credential provider returned from the app can be used with aws-sdk clients
    ComPtr<IApp> app = App::Create();

    // attempt to download a file from S3.
    {
        Aws::Client::ClientConfiguration config;
        Aws::S3::S3Client s3Client(app->CredentialProvider(), config, Aws::Client::AWSAuthV4Signer::PayloadSigningPolicy::Never, false);
        auto outcome = s3Client.ListBuckets();
        ASSERT_TRUE(outcome.IsSuccess());
        ASSERT_TRUE(outcome.GetResult().GetBuckets().size() > 0);
    }
}

TEST(PanoramaAppTests, PropertyDelegateHandlers)
{
    UnsetEnvVar("MDS_IP_OVERRIDE");

    HRESULT hr = S_OK;
    
    // =============== S3 Property Delegate =======================
    {
        // Valid input, just non valid resource
        CommandLineArgs args = CreateCommandLineArgs("exeName --PropertyDelegates [{\"type\":\"s3\",\"bucket\":\"b\",\"key\":\"k\",\"region\":\"us-west-2\"}]");
        ComPtr<IApp> app = App::CreateWithArgs(args.Count(), args.Values());
        ASSERT_TRUE(app == nullptr);
    }

    {
        // invalid bucket
        CommandLineArgs args = CreateCommandLineArgs("exeName --PropertyDelegates [{\"type\":\"s3\",\"bucket\":5,\"key\":\"k\",\"region\":\"us-west-2\"}]");
        ComPtr<IApp> app = App::CreateWithArgs(args.Count(), args.Values());
        ASSERT_TRUE(app == nullptr);
    }

    {
        // invalid key
        CommandLineArgs args = CreateCommandLineArgs("exeName --PropertyDelegates [{\"type\":\"s3\",\"bucket\":\"b\",\"key\":5,\"region\":\"us-west-2\"}]");
        ComPtr<IApp> app = App::CreateWithArgs(args.Count(), args.Values());
        ASSERT_TRUE(app == nullptr);
    }

    {
        // invalid region
        CommandLineArgs args = CreateCommandLineArgs("exeName --PropertyDelegates [{\"type\":\"s3\",\"bucket\":\"b\",\"key\":\"k\",\"region\":5}]");
        ComPtr<IApp> app = App::CreateWithArgs(args.Count(), args.Values());
        ASSERT_TRUE(app == nullptr);
    }

    {
        // Valid input
        CommandLineArgs args = CreateCommandLineArgs("exeName --PropertyDelegates [{\"type\":\"s3\",\"bucket\":\"panorama-sdk-v2-artifacts\",\"key\":\"testdata/s3propertydelegate/s3PropertyDelegateTest\",\"region\":\"us-west-2\"}]");
        ComPtr<IApp> app = App::CreateWithArgs(args.Count(), args.Values());
        ASSERT_TRUE(app != nullptr);
    }

    // ==================== IoT Shadow Property Delegate =====================
    {
        // Valid input, just non valid resource
        CommandLineArgs args = CreateCommandLineArgs("exeName --PropertyDelegates [{\"type\":\"iot\",\"thingName\":\"t\",\"shadowName\":\"s\",\"region\":\"us-west-2\"}]");
        ComPtr<IApp> app = App::CreateWithArgs(args.Count(), args.Values());
        ASSERT_TRUE(app == nullptr);
    }

    {
        // invalid thingName
        CommandLineArgs args = CreateCommandLineArgs("exeName --PropertyDelegates [{\"type\":\"iot\",\"thingName\":0,\"shadowName\":\"s\",\"region\":\"us-west-2\"}]");
        ComPtr<IApp> app = App::CreateWithArgs(args.Count(), args.Values());
        ASSERT_TRUE(app == nullptr);
    }

    {
        // invalid shadowName
        CommandLineArgs args = CreateCommandLineArgs("exeName --PropertyDelegates [{\"type\":\"iot\",\"thingName\":\"t\",\"shadowName\":0,\"region\":\"us-west-2\"}]");
        ComPtr<IApp> app = App::CreateWithArgs(args.Count(), args.Values());
        ASSERT_TRUE(app == nullptr);
    }

    {
        // invalid region
        CommandLineArgs args = CreateCommandLineArgs("exeName --PropertyDelegates [{\"type\":\"iot\",\"thingName\":\"t\",\"shadowName\":\"s\",\"region\":0}]");
        ComPtr<IApp> app = App::CreateWithArgs(args.Count(), args.Values());
        ASSERT_TRUE(app == nullptr);
    }

    {
        // Valid input
        CommandLineArgs args = CreateCommandLineArgs("exeName --PropertyDelegates [{\"type\":\"iot\",\"thingName\":\"sdk_v2_test\",\"shadowName\":\"DeviceTests_PropertyDelegateHandlers\",\"region\":\"us-west-2\"}]");
        ComPtr<IApp> app = App::CreateWithArgs(args.Count(), args.Values());
        ASSERT_TRUE(app != nullptr);

        ComPtr<IStringProperty> property;
        ASSERT_S(app->GetStringProperty(property.AddressOf(), "welcome"));
    }

    {
        // Valid input, without optional input
        CommandLineArgs args = CreateCommandLineArgs("exeName --PropertyDelegates [{\"type\":\"iot\",\"thingName\":\"sdk_v2_test\",\"region\":\"us-west-2\"}]");
        ComPtr<IApp> app = App::CreateWithArgs(args.Count(), args.Values());
        ASSERT_TRUE(app != nullptr);

        ComPtr<IStringProperty> property;
        ASSERT_S(app->GetStringProperty(property.AddressOf(), "pipelines"));
    }
}

TEST(PanoramaAppTests, Synchronize)
{
    HRESULT hr = S_OK;

    std::string contents1 = "{\"var1\":\"hello\", \"var2\":\"world\"}";
    std::string contents2 = "{\"var3\":\"foo\", \"var4\":\"bar\"}";

    std::string file1 = (BuildDirectory()+"synchronize_test_file1.json").c_str();
    std::string file2 = (BuildDirectory()+"synchronize_test_file2.json").c_str();

    FILE* fptr = fopen(file1.c_str(), "w");
    fwrite(contents1.c_str(), sizeof(char), contents1.size(), fptr);
    fclose(fptr);

    fptr = fopen(file2.c_str(), "w");
    fwrite(contents2.c_str(), sizeof(char), contents2.size(), fptr);
    fclose(fptr);

    ComPtr<IPropertyDelegate> delegate1, delegate2;
    ASSERT_S(CreateFilePropertyDelegate(delegate1.AddressOf(), file1.c_str()));
    ASSERT_S(CreateFilePropertyDelegate(delegate2.AddressOf(), file2.c_str()));

    ComPtr<IApp> app = App::Create();
    ASSERT_S(app->AddPropertyDelegate(delegate1));
    ASSERT_S(app->AddPropertyDelegate(delegate2));
    
    contents1 = "{\"var1\":\"hello\", \"var2\":\"world2\"}";
    contents2 = "{\"var3\":\"foo2\", \"var4\":\"bar\"}";

    fptr = fopen(file1.c_str(), "w");
    fwrite(contents1.c_str(), sizeof(char), contents1.size(), fptr);
    fclose(fptr);

    fptr = fopen(file2.c_str(), "w");
    fwrite(contents2.c_str(), sizeof(char), contents2.size(), fptr);
    fclose(fptr);

    ComPtr<IPropertyCollection> changed_properties;
    ASSERT_S(app->Synchronize(changed_properties.AddressOf()));
    ASSERT_EQ(changed_properties->Count(), 2);
    ASSERT_TRUE(changed_properties->ContainsKey("var2"));
    ASSERT_TRUE(changed_properties->ContainsKey("var3"));
}