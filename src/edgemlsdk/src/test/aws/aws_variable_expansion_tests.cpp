#include <string>
#include <cstdio>
#include <fstream>
#include <gtest/gtest.h>
#if __has_include(<filesystem>)
    #include <filesystem>
    namespace fs = std::filesystem;
#elif __has_include(<experimental/filesystem>)
    #include <experimental/filesystem>
    namespace fs = std::experimental::filesystem;
#else
  error "Missing the <filesystem>/<experimental/filesystem> header."
#endif
#include <aws/crt/Api.h>
#include <aws/secretsmanager/SecretsManagerClient.h>
#include <aws/secretsmanager/model/PutSecretValueRequest.h>
#include <aws/secretsmanager/model/PutSecretValueResult.h>
#include <aws/core/utils/ARN.h>
#include <nlohmann/json.hpp>

#include <Panorama/properties.h>
#include <Panorama/credentials.h>
#include <Panorama/aws.h>
#include <Panorama/eventing.h>

#include <TestUtils.h>

using namespace Panorama;

std::shared_ptr<Aws::SecretsManager::SecretsManagerClient> CreateSMClient(const char* arnString, ICredentialProvider* cred_provider)
{
    HRESULT hr = S_OK;
    CHECKNULL(arnString, nullptr);

    // Use AWS Apis to fetch secret
    Aws::Client::ClientConfiguration config;

    // Get the region from the arn so it can specified on the config
    Aws::Utils::ARN arn(arnString);
    config.region = arn.GetRegion();

    // Create the secrets manager client
    std::shared_ptr<Aws::SecretsManager::SecretsManagerClient> sm_client;
    sm_client = std::make_shared<Aws::SecretsManager::SecretsManagerClient>(cred_provider->CredentialProvider(), config);

    return sm_client;
}

HRESULT WriteSecretsManagerValue(const char* arn, const char* val, ICredentialProvider* cred_provider)
{
    HRESULT hr = S_OK;
    TraceVerbose("Writing secret at %s", arn);

    std::shared_ptr<Aws::SecretsManager::SecretsManagerClient> sm_client = CreateSMClient(arn, cred_provider);
    CHECKNULL(sm_client, E_FAIL);

    // Create a CreateSecretRequest with the secret name and value
    Aws::SecretsManager::Model::PutSecretValueRequest request;
    request.SetSecretId(arn);
    request.SetSecretString(val);

    // Put the secret value
    Aws::SecretsManager::Model::PutSecretValueOutcome result = sm_client->PutSecretValue(request);
    if (result.IsSuccess() == false) 
    {
        TraceError("Aws::SecretsManager::Model::PutSecretValueOutcome does not indicate success: %s", result.GetError().GetMessage().c_str());
        return E_FAIL;
    } 

    return hr;
}

void SecretsManagerExpansionTest()
{
    HRESULT hr = S_OK;
    ComPtr<ICredentialProvider> credProvider = Panorama_Aws::DefaultCredentialProvider();

    {
        ComPtr<IStringProperty> property;
        ASSERT_S(CreateStringProperty(property.AddressOf(), "id", "{\"type\":\"secretsmanager\", \"value\":\"arn:aws:secretsmanager:us-west-2:691462484548:secret:panorama-sdk-v2/test/test_camera_credential-8tWMKC;username\"}"));

        ComPtr<IVariableExpansion> variable;
        ASSERT_S(CreateSecretsManagerExpansion(variable.AddressOf(), property, credProvider));

        ComPtr<IBuffer> expansionBuffer;
        ASSERT_S(variable->Expand(expansionBuffer.AddressOf()));
        std::string expansion = expansionBuffer->AsString();
        ASSERT_FALSE(expansion.compare("MyUsername"));
        ASSERT_FALSE(variable->Immutable());

        // Just write the same content
        nlohmann::json new_value;
        new_value["username"] = "MyUsername";
        new_value["password"] = "MyPassword";
        ASSERT_FALSE(variable->Stale());
        ASSERT_S(WriteSecretsManagerValue("arn:aws:secretsmanager:us-west-2:691462484548:secret:panorama-sdk-v2/test/test_camera_credential-8tWMKC", new_value.dump().c_str(), credProvider));
        ASSERT_TRUE(variable->Stale());

        property->Set("{\"type\":\"secretsmanager\", \"value\":\"invalid_arn\"}");
        ComPtr<IBuffer> expansionBuffer2;
        ASSERT_F(variable->Expand(expansionBuffer2.AddressOf()));
    }
}

HRESULT VerifyDownloadedFile(const std::string& filePath, const std::string& expectedContent)
{
    HRESULT hr = S_OK;
    // Check if the file exists
    CHECKIF_MSG(fs::exists(filePath) == false, E_NOT_FOUND, "Cannot find downloaded file");
    // Check file content 
    std::ifstream file(filePath);
    CHECKIF_MSG(file.is_open() == false, E_FAIL, "Failed to open downloaded file");
    std::string fileContent((std::istreambuf_iterator<char>(file)), std::istreambuf_iterator<char>());
    file.close();
    CHECKIF_MSG(fileContent != expectedContent, E_FAIL, "The content of downloaded file does not match the expected content");
    // Remove the file
    CHECKIF_MSG(std::remove(filePath.c_str()) != 0, E_FAIL, "Failed to remove downloaded file");
    return hr;
}

void S3ExpansionTest()
{
    fs::current_path(BuildDirectory());

    HRESULT hr = S_OK;
    std::string S3_FILE_NAME = "S3VarExpTest";
    std::string S3_FILE_NAME_2 = "S3VarExpTest2";
    std::string S3_FILE_NAME_CONTENT = "{\"s3String\":\"s3\"}";
    std::string currentDir = std::string(BuildDirectory()) + "/s3_expansion_test/";
    ComPtr<ICredentialProvider> credProvider = Panorama_Aws::DefaultCredentialProvider();

    ComPtr<IStringProperty> property;
    nlohmann::json value;
    value["bucket"] = "panorama-sdk-v2-artifacts";
    value["key"] = "test/" + S3_FILE_NAME;
    value["region"] = "us-west-2";
    value["destination"] = currentDir + "localFile";

    nlohmann::json contents;
    contents["type"] = "s3";
    contents["value"] = value;

    ASSERT_S(CreateStringProperty(property.AddressOf(), "id", contents.dump().c_str()));

    ComPtr<IVariableExpansion> variable;
    ASSERT_S(CreateS3Expansion(variable.AddressOf(), property, credProvider));

    {
        // valid download
        TraceInfo("-- test 1 --");
        ComPtr<IBuffer> expansionBuffer;
        ASSERT_S(variable->Expand(expansionBuffer.AddressOf()));
        std::string expansion = expansionBuffer->AsString();
        std::string localFilePath = currentDir + "localFile";
        ASSERT_EQ(expansion.compare(localFilePath), 0);
        ASSERT_S(VerifyDownloadedFile(localFilePath, S3_FILE_NAME_CONTENT));
    }
    {
        // non-existing s3 arn
        TraceInfo("-- test 3 --");

        contents["value"]["bucket"] = "non-existent";
        contents["value"]["key"] = "non-existent";
        contents["value"]["region"] = "us-west-2";
        contents["value"]["destination"] = currentDir + S3_FILE_NAME;
        ASSERT_S(property->Set(contents.dump().c_str()));

        ComPtr<IBuffer> expansionBuffer;
        ASSERT_F(variable->Expand(expansionBuffer.AddressOf()));
    }
    {
        // download file to a directory 
        TraceInfo("-- test 4 --");

        contents["value"]["bucket"] = "panorama-sdk-v2-artifacts";
        contents["value"]["key"] = "test/" + S3_FILE_NAME;
        contents["value"]["region"] = "us-west-2";
        contents["value"]["destination"] = currentDir;
        ASSERT_S(property->Set(contents.dump().c_str()));

        ComPtr<IBuffer> expansionBuffer;
        ASSERT_S(variable->Expand(expansionBuffer.AddressOf()));
        std::string expansion = expansionBuffer->AsString();
        std::string localFilePath = currentDir + S3_FILE_NAME;
        ASSERT_EQ(expansion.compare(localFilePath), 0);
        ASSERT_S(VerifyDownloadedFile(localFilePath, S3_FILE_NAME_CONTENT));
    }
    {
        // download file to a non-existing directory 
        TraceInfo("-- test 5 --");

        contents["value"]["bucket"] = "panorama-sdk-v2-artifacts";
        contents["value"]["key"] = "test/" + S3_FILE_NAME;
        contents["value"]["region"] = "us-west-2";
        contents["value"]["destination"] = currentDir + "non-existing/";
        ASSERT_S(property->Set(contents.dump().c_str()));

        ComPtr<IBuffer> expansionBuffer;
        ASSERT_S(variable->Expand(expansionBuffer.AddressOf()));
        std::string expansion = expansionBuffer->AsString();
        std::string localFilePath = currentDir + "non-existing/" + S3_FILE_NAME;
        ASSERT_EQ(expansion.compare(localFilePath), 0);
        ASSERT_S(VerifyDownloadedFile(localFilePath, S3_FILE_NAME_CONTENT));
    }
    {
        // use relative local path
        TraceInfo("-- test 6 --");

        contents["value"]["bucket"] = "panorama-sdk-v2-artifacts";
        contents["value"]["key"] = "test/" + S3_FILE_NAME;
        contents["value"]["region"] = "us-west-2";
        contents["value"]["destination"] = "./s3_expansion_test/";
        ASSERT_S(property->Set(contents.dump().c_str()));

        ComPtr<IBuffer> expansionBuffer;
        ASSERT_S(variable->Expand(expansionBuffer.AddressOf()));
        std::string expansion = expansionBuffer->AsString();
        std::string localFilePath = currentDir + S3_FILE_NAME;
        ASSERT_EQ(expansion, localFilePath);
        ASSERT_S(VerifyDownloadedFile(localFilePath, S3_FILE_NAME_CONTENT));
    }
    {
        // valid s3 arn points to a directory
        TraceInfo("-- test 7 --");

        contents["value"]["bucket"] = "panorama-sdk-v2-artifacts";
        contents["value"]["key"] = "test/";
        contents["value"]["region"] = "us-west-2";
        contents["value"]["destination"] = "./s3_expansion_test/";
        ASSERT_S(property->Set(contents.dump().c_str()));

        ComPtr<IBuffer> expansionBuffer;
        ASSERT_S(variable->Expand(expansionBuffer.AddressOf()));
        std::string expansion = expansionBuffer->AsString();
        ASSERT_EQ(expansion, currentDir);

        //TODO: remove hardcode
        std::string localFilePath = currentDir + "/" + S3_FILE_NAME;
        ASSERT_S(VerifyDownloadedFile(localFilePath, S3_FILE_NAME_CONTENT));
        localFilePath = currentDir + "/" + S3_FILE_NAME_2;
        ASSERT_S(VerifyDownloadedFile(localFilePath, S3_FILE_NAME_CONTENT));
    }
    {
        // s3 arn pointing to a directory doesn't end with '/'
        TraceInfo("-- test 8 --");

        contents["value"]["bucket"] = "panorama-sdk-v2-artifacts";
        contents["value"]["key"] = "test";
        contents["value"]["region"] = "us-west-2";
        contents["value"]["destination"] = "./s3_expansion_test/";
        ASSERT_S(property->Set(contents.dump().c_str()));

        ComPtr<IBuffer> expansionBuffer;
        ASSERT_F(variable->Expand(expansionBuffer.AddressOf()));
    }
    {
        // localpath is not a directory when s3 arn points to a directory
        TraceInfo("-- test 9 --");

        contents["value"]["bucket"] = "panorama-sdk-v2-artifacts";
        contents["value"]["key"] = "test/";
        contents["value"]["region"] = "us-west-2";
        contents["value"]["destination"] = "./s3_expansion_test/file";
        ASSERT_S(property->Set(contents.dump().c_str()));

        ComPtr<IBuffer> expansionBuffer;
        ASSERT_F(variable->Expand(expansionBuffer.AddressOf()));
    }
}

TEST(AWSTests, VariableExpansions)
{
    SecretsManagerExpansionTest();
    S3ExpansionTest();
}