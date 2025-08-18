// standard headers
#include <thread>

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

TEST(PanoramaAppTests, StandaloneClientInitialize)
{
    UnsetEnvVar("MDS_IP_OVERRIDE");

    HRESULT hr = S_OK;
    ComPtr<AppRequestHandler> handler;
    GetAppRequestHandler(handler.AddressOf());

    ComPtr<IApp> app = App::Create();
    ASSERT_TRUE(app);

    ASSERT_FALSE(handler->AnnounceSelfCalled.WaitFor(0));
}

TEST(PanoramaAppTests, MDSClientInitialize)
{
    SetEnvVar("MDS_IP_OVERRIDE", "127.0.0.1:8089");
    SetEnvVar("Node_Uid", "test_node");

    HRESULT hr = S_OK;
    ComPtr<AppRequestHandler> handler;
    GetAppRequestHandler(handler.AddressOf());

    ComPtr<IApp> app = App::Create();
    ASSERT_TRUE(app);

    ASSERT_TRUE(handler->AnnounceSelfCalled.WaitFor(0));
    ASSERT_TRUE(handler->HeartbeatCalled.WaitFor(3000));
    ASSERT_FALSE(strcmp(handler->HeartbeatNodeId.c_str(), "test_node"));
    ASSERT_FALSE(strcmp(handler->HeartbeatErrorCode.c_str(), "NONE"));
    ASSERT_FALSE(strcmp(handler->HeartbeatStatus.c_str(), "ACTIVE"));
}

TEST(PanoramaAppTests, CLIProperties)
{
    HRESULT hr = S_OK;
    CommandLineArgs args = CreateCommandLineArgs("exeName --myProperty 5");

    ComPtr<IApp> app = App::CreateWithArgs(args.Count(), args.Values());
    ASSERT_TRUE(app);

    ComPtr<IIntegerProperty> intProp;
    ASSERT_S(app->GetIntegerProperty(intProp.AddressOf(), "myProperty"));
    EXPECT_EQ(intProp->Get(), 5);
}

TEST(PanoramaAppTests, DefaultCredentialProvider)
{
    UnsetEnvVar("MDS_IP_OVERRIDE");

    std::string key = GetEnvVar("AWS_ACCESS_KEY_ID");
    std::string secret = GetEnvVar("AWS_SECRET_ACCESS_KEY");
    std::string token = GetEnvVar("AWS_SESSION_TOKEN");

    SetEnvVar("AWS_ACCESS_KEY_ID", "key");
    SetEnvVar("AWS_SECRET_ACCESS_KEY", "secret");
    SetEnvVar("AWS_SESSION_TOKEN", "token");

    ComPtr<IApp> app = App::Create();
    Aws::Auth::AWSCredentials creds = app->GetAWSCredentials();

    EXPECT_EQ(creds.GetAWSAccessKeyId(), "key");
    EXPECT_EQ(creds.GetAWSSecretKey(), "secret");
    EXPECT_EQ(creds.GetSessionToken(), "token");

    SetEnvVar("AWS_ACCESS_KEY_ID", key);
    SetEnvVar("AWS_SECRET_ACCESS_KEY", secret);
    SetEnvVar("AWS_SESSION_TOKEN", token);
}

TEST(PanoramaAppTests, MDSCredentialProvider)
{
    SetEnvVar("MDS_IP_OVERRIDE", "127.0.0.1:8089");
    SetEnvVar("Node_Uid", "test_node");

    ComPtr<AppRequestHandler> handler;
    GetAppRequestHandler(handler.AddressOf());
    handler->SetCredentials("{\"accessKeyId\":\"key\",\"secretAccessKey\":\"secret\",\"sessionToken\":\"token\",\"expiration\":1665600769000}");

    ComPtr<IApp> app = App::Create();
    Aws::Auth::AWSCredentials creds = app->GetAWSCredentials();

    EXPECT_EQ(creds.GetAWSAccessKeyId(), "key");
    EXPECT_EQ(creds.GetAWSSecretKey(), "secret");
    EXPECT_EQ(creds.GetSessionToken(), "token");
    EXPECT_EQ(creds.GetExpiration().Millis(), 1665600769000);
}

TEST(PanoramaAppTests, MDSPropertyDelegate)
{
    HRESULT hr = S_OK;

    SetEnvVar("MDS_IP_OVERRIDE", "127.0.0.1:8089");
    SetEnvVar("Node_Uid", "test_node");

    ComPtr<AppRequestHandler> handler;
    GetAppRequestHandler(handler.AddressOf());
    handler->SetPorts("{\"inputPortList\":[{\"name\":\"testString\",\"type\":\"STRING\",\"value\":{\"type\":\"STRING\",\"dataList\":[\"This is a test string\"]}},{\"name\":\"testInteger\",\"type\":\"INT32\",\"value\":{\"type\":\"INT32\",\"dataList\":[\"8\"]}},{\"name\":\"testFloat\",\"type\":\"FLOAT32\",\"value\":{\"type\":\"FLOAT32\",\"dataList\":[\"16.0f\"]}},{\"name\":\"testBoolean\",\"type\":\"BOOLEAN\",\"value\":{\"type\":\"BOOLEAN\",\"dataList\":[\"true\"]}}]}");

    ComPtr<IApp> app = App::Create();
    
    ComPtr<IStringProperty> stringProperty;
    ASSERT_S(app->GetStringProperty(stringProperty.AddressOf(), "testString"));
    EXPECT_FALSE(strcmp(stringProperty->Get(), "This is a test string"));

    ComPtr<IIntegerProperty> intProperty;
    ASSERT_S(app->GetIntegerProperty(intProperty.AddressOf(), "testInteger"));
    EXPECT_EQ(intProperty->Get(), 8);

    ComPtr<IFloatProperty> floatProperty;
    ASSERT_S(app->GetFloatProperty(floatProperty.AddressOf(), "testFloat"));
    EXPECT_EQ(floatProperty->Get(), 16.0f);

    ComPtr<IBooleanProperty> boolProperty;
    ASSERT_S(app->GetBooleanProperty(boolProperty.AddressOf(), "testBoolean"));
    EXPECT_EQ(boolProperty->Get(), true);

    ComPtr<IBooleanProperty> noInterfaceProperty;
    EXPECT_EQ(E_NOINTERFACE, app->GetBooleanProperty(noInterfaceProperty.AddressOf(), "testString"));

    ComPtr<IBooleanProperty> notFoundProperty;
    EXPECT_EQ(E_NOT_FOUND, app->GetBooleanProperty(notFoundProperty.AddressOf(), "nonExistent"));
}