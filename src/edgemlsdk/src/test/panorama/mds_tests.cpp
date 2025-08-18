// standard headers
#include <thread>

// depednencies headers
#include <gtest/gtest.h>

// Panorama public headers
#include <Panorama/comobj.h>
#include <Panorama/comptr.h>
#include <Panorama/eventing.h>
#include <Panorama/app.h>

// Panorama private headers
#include <tools/mockdevice/mock_panorama_device.h>
#include <TestUtils.h>
#include <test_app_request_handler.h>

using namespace std;
using namespace Panorama;


TEST(PanoramAppTests, MDS_GetCredentials)
{
    HRESULT hr = S_OK;

    ComPtr<AppRequestHandler> handler;
    GetAppRequestHandler(handler.AddressOf());

    Aws::Auth::AWSCredentials creds;
    {
        ComPtr<IMDSClient> mds_client = MDS::Client("127.0.0.1", 8089, "test node");
        ASSERT_TRUE(mds_client);

        ComPtr<ICredentialProvider> provider = MDS::CredentialProvider(mds_client);
        ASSERT_TRUE(provider);

        handler->SetCredentials("{\"accessKeyId\":\"key\",\"secretAccessKey\":\"secret\",\"sessionToken\":\"token\",\"expiration\":1665600769000}");
        creds = provider->GetAWSCredentials();
        EXPECT_EQ(creds.GetAWSAccessKeyId(), "key");
        EXPECT_EQ(creds.GetAWSSecretKey(), "secret");
        EXPECT_EQ(creds.GetSessionToken(), "token");
        EXPECT_EQ(creds.GetExpiration().Millis(), 1665600769000);
        ASSERT_TRUE(handler->GetCredentialsCalled.WaitFor(0));

        handler->SetCredentials("{\"accessKeyId\":\"key\",\"secretAccessKey\":\"secret\",\"sessionToken\":\"token\",\"expiration\":\"2022-10-11T18:14:52Z\"}");
        creds = provider->GetAWSCredentials();
        EXPECT_EQ(creds.GetAWSAccessKeyId(), "key");
        EXPECT_EQ(creds.GetAWSSecretKey(), "secret");
        EXPECT_EQ(creds.GetSessionToken(), "token");
        EXPECT_EQ(creds.GetExpiration().ToGmtString(Aws::Utils::DateFormat::ISO_8601), "2022-10-11T18:14:52Z");
        ASSERT_TRUE(handler->GetCredentialsCalled.WaitFor(0));

        handler->SetCredentials("{\"AccessKeyId\":\"key\",\"SecretAccessKey\":\"secret\",\"SessionToken\":\"token\",\"Expiration\":\"2022-10-11T18:14:52Z\"}");
        creds = provider->GetAWSCredentials();
        EXPECT_EQ(creds.GetAWSAccessKeyId(), "key");
        EXPECT_EQ(creds.GetAWSSecretKey(), "secret");
        EXPECT_EQ(creds.GetSessionToken(), "token");
        EXPECT_EQ(creds.GetExpiration().ToGmtString(Aws::Utils::DateFormat::ISO_8601), "2022-10-11T18:14:52Z");
        ASSERT_TRUE(handler->GetCredentialsCalled.WaitFor(0));

        handler->SetCredentials("{\"AccessKeyId\":\"key\",\"SecretAccessKey\":\"secret\",\"SessionToken\":\"token\",\"Expiration\":\"2022-10-11T18:14:52+0000\"}");
        creds = provider->GetAWSCredentials();
        EXPECT_EQ(creds.GetAWSAccessKeyId(), "key");
        EXPECT_EQ(creds.GetAWSSecretKey(), "secret");
        EXPECT_EQ(creds.GetSessionToken(), "token");
        EXPECT_EQ(creds.GetExpiration().ToGmtString(Aws::Utils::DateFormat::ISO_8601), "2022-10-11T18:14:52Z");
        ASSERT_TRUE(handler->GetCredentialsCalled.WaitFor(0));

        handler->SetCredentials("{\"AccessKeyId\":\"ASIA...\",\"SecretAccessKey\":\"l+zXk...\",\"Token\":\"IQoJb3JpZ2luX2VjEOL//////////...\",\"Expiration\":\"2022-10-13T18:55:29+0000\"}");
        creds = provider->GetAWSCredentials();
        EXPECT_EQ(creds.GetAWSAccessKeyId(), "ASIA...");
        EXPECT_EQ(creds.GetAWSSecretKey(), "l+zXk...");
        EXPECT_EQ(creds.GetSessionToken(), "IQoJb3JpZ2luX2VjEOL//////////...");
        EXPECT_EQ(creds.GetExpiration().ToGmtString(Aws::Utils::DateFormat::ISO_8601), "2022-10-13T18:55:29Z");

        // setting credentials to expire on March-5-2159
        handler->SetCredentials("{\"accessKeyId\": \"key\", \"secretAccessKey\": \"secret\",\"sessionToken\": \"token\",\"expiration\": 5969779292000}");

        // credentials in app are expired, so calling GetAWSCredentials again should call into the MDS
        creds = provider->GetAWSCredentials();
        ASSERT_TRUE(handler->GetCredentialsCalled.WaitFor(0));

        // credentials in app are no longer expired, should return cached, so calling GetAWSCredentials should NOT call into MDS
        creds = provider->GetAWSCredentials();
        ASSERT_FALSE(handler->GetCredentialsCalled.WaitFor(0));
    }
}

TEST(PanoramAppTests, PropertyDelegate)
{
    HRESULT hr = S_OK;

    ComPtr<AppRequestHandler> handler;
    GetAppRequestHandler(handler.AddressOf());

    // No input parameters
    {
        handler->SetPorts("{\"inputPortList\":[]}");

        ComPtr<IMDSClient> mds_client = MDS::Client("127.0.0.1", 8089, "test node");
        ASSERT_TRUE(mds_client);
        ComPtr<IPropertyDelegate> delegate = MDS::PropertyDelegate(mds_client);

        ComPtr<IProperty> property;
        EXPECT_EQ(E_NOT_FOUND, delegate->GetProperty(property.AddressOf(), "testString"));
    }

    {
        handler->SetPorts("{\"inputPortList\":[{\"name\":\"testString\",\"type\":\"STRING\",\"value\":{\"type\":\"STRING\",\"dataList\":[\"This is a test string\"]}},{\"name\":\"testInteger\",\"type\":\"INT32\",\"value\":{\"type\":\"INT32\",\"dataList\":[\"8\"]}},{\"name\":\"testFloat\",\"type\":\"FLOAT32\",\"value\":{\"type\":\"FLOAT32\",\"dataList\":[\"16.0f\"]}},{\"name\":\"testBoolean\",\"type\":\"BOOLEAN\",\"value\":{\"type\":\"BOOLEAN\",\"dataList\":[\"true\"]}}]}");
        ComPtr<IMDSClient> mds_client = MDS::Client("127.0.0.1", 8089, "test node");
        ASSERT_TRUE(mds_client);
        ComPtr<IPropertyDelegate> delegate = MDS::PropertyDelegate(mds_client);

        ComPtr<IProperty> stringProperty;
        ASSERT_S(delegate->GetProperty(stringProperty.AddressOf(), "testString"));
        ASSERT_TRUE(stringProperty.QueryInterface<IStringProperty>());
        EXPECT_FALSE(strcmp(stringProperty.QueryInterface<IStringProperty>()->Get(), "This is a test string"));

        ComPtr<IProperty> intProperty;
        ASSERT_S(delegate->GetProperty(intProperty.AddressOf(), "testInteger"));
        ASSERT_TRUE(intProperty.QueryInterface<IIntegerProperty>());
        EXPECT_EQ(intProperty.QueryInterface<IIntegerProperty>()->Get(), 8);

        ComPtr<IProperty> floatProperty;
        ASSERT_S(delegate->GetProperty(floatProperty.AddressOf(), "testFloat"));
        ASSERT_TRUE(floatProperty.QueryInterface<IFloatProperty>());
        EXPECT_EQ(floatProperty.QueryInterface<IFloatProperty>()->Get(), 16.0f);

        ComPtr<IProperty> boolProperty;
        ASSERT_S(delegate->GetProperty(boolProperty.AddressOf(), "testBoolean"));
        ASSERT_TRUE(boolProperty.QueryInterface<IBooleanProperty>());
        EXPECT_EQ(boolProperty.QueryInterface<IBooleanProperty>()->Get(), true);
    }
}

TEST(PanoramAppTests, AnnounceSelf)
{
    HRESULT hr = S_OK;

    ComPtr<AppRequestHandler> handler;
    GetAppRequestHandler(handler.AddressOf());

    ComPtr<IMDSClient> mds_client = MDS::Client("127.0.0.1", 8089, "test node");
    ASSERT_TRUE(mds_client);

    ASSERT_S(mds_client->AnnounceSelf());
    ASSERT_TRUE(handler->AnnounceSelfCalled.WaitFor(0));
    ASSERT_FALSE(strcmp(handler->AnnounceSelfNodeId.c_str(), "test node"));
    ASSERT_FALSE(strcmp(handler->AnnounceSelfVersion.c_str(), "v1"));
}

TEST(PanoramAppTests, Heartbeat)
{
    HRESULT hr = S_OK;

    ComPtr<AppRequestHandler> handler;
    GetAppRequestHandler(handler.AddressOf());

    ComPtr<IMDSClient> mds_client = MDS::Client("127.0.0.1", 8089, "test node");
    ASSERT_TRUE(mds_client);

    ASSERT_S(mds_client->Heartbeat("my error code", "status"));
    ASSERT_TRUE(handler->HeartbeatCalled.WaitFor(0));
    ASSERT_FALSE(strcmp(handler->HeartbeatNodeId.c_str(), "test node"));
    ASSERT_FALSE(strcmp(handler->HeartbeatErrorCode.c_str(), "my error code"));
    ASSERT_FALSE(strcmp(handler->HeartbeatStatus.c_str(), "status"));
}