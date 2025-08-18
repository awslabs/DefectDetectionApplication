#include <thread>
#include <iostream>
#include <fstream>
#include <regex>

#include <aws/s3/S3Client.h>
#include <aws/s3/model/ListObjectsV2Request.h>
#include <aws/core/Aws.h>
#include <aws/core/utils/ARN.h>
#include <aws/core/utils/threading/Executor.h>
#include <aws/core/platform/FileSystem.h>
#include <aws/core/utils/StringUtils.h>
#include <aws/transfer/TransferManager.h>
#include <aws/transfer/TransferHandle.h>
#include <nlohmann/json.hpp>

#include <Panorama/comptr.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/eventing.h>
#include <Panorama/aws.h>
#include <Panorama/properties.h>
#include <Panorama/buffer.h>
#include <Panorama/credentials.h>
#include <core/property/expansion_base.h>
#include <misc.h>
#include <filesystem_safe.h>

using namespace Panorama;

#define MAX_RETRY_ATTEMPT 3
#define TRANSFER_EXECUTOR_THREADS 10 
#define S3_EXECUTOR_THREADS 5
#define CONNECTION_TIMEOUT_MS 5000
#define REQUEST_TIMEOUT_MS 100000

class S3Expansion : public ExpansionBase<nlohmann::json>
{
public:
    static HRESULT Create(IVariableExpansion** ppObj, IStringProperty* property, ICredentialProvider* credential_provider)
    {
        COM_FACTORY(S3Expansion, Initialize(property, credential_provider));
    }

    ~S3Expansion()
    {
        COM_DTOR(S3Expansion);
        _directoryDownloadSignalReceived.Set();
        // Forcing this to be destroyed before AwsContext is destroyed
        // If AwsContext goes out of scope first and that was the only reference
        // than Aws SDK will crash when trying to delete _client....0 out of 10!
        _s3Client.reset();
        _transferManagerClient.reset();
        COM_DTOR_FIN(S3Expansion);
    }

    HRESULT Expand(IBuffer** ppObj) override
    {
        HRESULT hr = S_OK;
        CHECKNULL(ppObj, E_POINTER);
        CHECKHR(Parse());

        TraceVerbose("Local Path is %s", _localPath.c_str());

        // Make absolute local path
        MakeAbsolutePath(_localPath);
        TraceVerbose("Absolute Local Path is %s", _localPath.c_str());

        // Create S3 Client
        CHECKHR(CreateS3Client());

        // Create TransferManager Client
        CHECKHR(CreateTransferManagerClient());

        bool isDirectory = _objectKey.back() ==  Aws::FileSystem::PATH_DELIM;
        if (isDirectory)
        {
            // verify local path is a directory
            CHECKIF_MSG(_localPath.back() != Aws::FileSystem::PATH_DELIM, E_FAIL, "When key refers to a directory(end with '/'), local path has to a directory(end with '/')");
            
            // Count files to download
            _keyCount = CountFiles(_s3Bucket, _objectKey);
            TraceInfo("There are %d files to download with s3 prefix [%s] from bucket [%s]", _keyCount, _objectKey.c_str(), _s3Bucket.c_str());

            // download directory recursively
            _transferManagerClient->DownloadToDirectory(_localPath, _s3Bucket, _objectKey);
            while(_directoryDownloadHandles.size() < _keyCount)
            {
                _directoryDownloadSignalReceived.Wait();
            }
            int downloadCount = 0;
            for (auto handle : _directoryDownloadHandles)
            {
                handle->WaitUntilFinished();
                CHECKHR(DownloadWithRetry(std::move(handle), MAX_RETRY_ATTEMPT));
                TraceInfo("Successfully download NO.%d file with s3 prefix [%s] from bucket [%s] to local path %s", downloadCount++, _objectKey.c_str(), _s3Bucket.c_str(), _localPath.c_str());
            }
        }
        else
        {
            // If localPath is a directory, append filename to the end
            if (_localPath.back() == Aws::FileSystem::PATH_DELIM)
            {
                std::string fileName;
                CHECKHR(ExtractFileNameFromObjectKey(_objectKey.c_str(), fileName));
                _localPath += fileName;
            }

            // If localPath contains non-existing directory, create the parent directory
            // DownloadFile() will not give any error when filename includes a non-existent directory
            // DownloadToDirectory() creates any needed destination directories
            // https://github.com/aws/aws-sdk-cpp/issues/578
            auto lastDelimter = _localPath.find_last_of(Aws::FileSystem::PATH_DELIM);
            if (lastDelimter != std::string::npos)
            {
                CHECKIF_MSG(Aws::FileSystem::CreateDirectoryIfNotExists(_localPath.substr(0, lastDelimter).c_str(), true) == false, E_FAIL, "localPath doesn't exist and failed to be created");
            }

            // download file
            std::shared_ptr<Aws::Transfer::TransferHandle> downloadHandle = _transferManagerClient->DownloadFile(_s3Bucket, _objectKey, [&]() { 
                TraceVerbose("Start to download...");
                return Aws::New<Aws::FStream>("S3VarExpansion", _localPath.c_str(), std::ios_base::out | std::ios_base::binary);
            });
            // auto status = downloadHandle->GetStatus();
            downloadHandle->WaitUntilFinished();
            CHECKHR(DownloadWithRetry(std::move(downloadHandle), MAX_RETRY_ATTEMPT));
            TraceInfo("Successfully download s3 object %s from bucket %s to local path %s", _objectKey.c_str(), _s3Bucket.c_str(), _localPath.c_str());
        }
        
        CHECKHR(CreateBufferFromString(ppObj, _localPath.c_str()));
        return hr;
    }

protected:
    HRESULT ParseValue(const nlohmann::json& value) override
    {
        CHECKIF_MSG(ValidateJsonProperty<const char*>(value, "bucket") == false, E_INVALIDARG, "bucket was not specified or is not a string");
        CHECKIF_MSG(ValidateJsonProperty<const char*>(value, "key") == false, E_INVALIDARG, "key was not specified or is not a string");
        CHECKIF_MSG(ValidateJsonProperty<const char*>(value, "region") == false, E_INVALIDARG, "region was not specified or is not a string");
        CHECKIF_MSG(ValidateJsonProperty<const char*>(value, "destination") == false, E_INVALIDARG, "destination was not specified or is not a string");

        _s3Bucket = value["bucket"];
        _region = value["region"];
        _objectKey = value["key"];
        _localPath = value["destination"];

        CHECKIF_MSG(_s3Bucket.empty(), E_INVALIDARG, "bucket cannot be empty");
        CHECKIF_MSG(_region.empty(), E_INVALIDARG, "region cannot be empty");
        CHECKIF_MSG(_objectKey.empty(), E_INVALIDARG, "key cannot be empty");
        CHECKIF_MSG(_localPath.empty(), E_INVALIDARG, "destination cannot be empty");

        return S_OK;
    }

private:
    HRESULT Initialize(IStringProperty* property, ICredentialProvider* credential_provider)
    {
        HRESULT hr = S_OK;
        CHECKHR(InitializeBase(property));
        CHECKNULL(credential_provider, E_INVALIDARG);
        CHECKHR(AwsContext(_aws_context.AddressOf()));

        _cred_provider = credential_provider;
        _s3Executor = Aws::MakeShared<Aws::Utils::Threading::PooledThreadExecutor>("executor-s3", S3_EXECUTOR_THREADS);
        _transferManagerExecutor = Aws::MakeShared<Aws::Utils::Threading::PooledThreadExecutor>("executor-transfer", TRANSFER_EXECUTOR_THREADS);
        
        CHECKHR(Parse());
        return hr;
    }

    HRESULT CreateS3Client()
    {
        HRESULT hr = S_OK;
        // Create the s3 client
        Aws::Client::ClientConfiguration s3ClientConfig;
        s3ClientConfig.connectTimeoutMs = CONNECTION_TIMEOUT_MS;
        s3ClientConfig.requestTimeoutMs = REQUEST_TIMEOUT_MS;
        s3ClientConfig.executor = _s3Executor;
        s3ClientConfig.region = _region;
        _s3Client = std::make_shared<Aws::S3::S3Client>(_cred_provider->CredentialProvider(), 
                                                        s3ClientConfig,
                                                        Aws::Client::AWSAuthV4Signer::PayloadSigningPolicy::RequestDependent,
                                                        true);
        CHECKNULL(_s3Client, E_FAIL);
        return hr;
    }

    HRESULT CreateTransferManagerClient()
    {
        HRESULT hr = S_OK;
        // Create transfer manager configuration
        Aws::Transfer::TransferManagerConfiguration transferConfig(_transferManagerExecutor.get());
        transferConfig.s3Client = _s3Client;
        // Set callback to receive initiated transfers for the directory operations
        transferConfig.transferInitiatedCallback = [&](const Aws::Transfer::TransferManager*, const std::shared_ptr<const Aws::Transfer::TransferHandle>& handle)
        {
            TraceVerbose("Download signal received");
            _directoryDownloadHandles.push_back(std::const_pointer_cast<Aws::Transfer::TransferHandle>(handle));
            if (_directoryDownloadHandles.size() == _keyCount)
            {
                _directoryDownloadSignalReceived.Set();
            }
        };
        _transferManagerClient = Aws::Transfer::TransferManager::Create(transferConfig);
        CHECKNULL(_transferManagerClient, E_FAIL);
        return hr;
    }

    HRESULT DownloadWithRetry(std::shared_ptr<Aws::Transfer::TransferHandle> handle, int16_t max_retry)
    {
        HRESULT hr = S_OK;

        int32_t retryAttempt = 0;
        while (handle->GetStatus() != Aws::Transfer::TransferStatus::COMPLETED)
        { 
            auto err = handle->GetLastError();
            TraceError("Failed to download s3 object [%s] from bucket [%s] to local path '%s'", _objectKey.c_str(), _s3Bucket.c_str(), _localPath.c_str());
            TraceError("Error: code [%d] message [%s]", static_cast<int>(err.GetResponseCode()), err.GetMessage().c_str()); 

            if(retryAttempt++ == max_retry)
                return E_FAIL;
            
            TraceInfo("NO.%d retry download...", retryAttempt);
            handle = _transferManagerClient->RetryDownload(handle); 
            handle->WaitUntilFinished();
        }
        return hr;
    }

    HRESULT ExtractFileNameFromObjectKey(const char* _objectKey, std::string& fileName)
    {
        CHECKNULL(_objectKey, E_INVALIDARG);
        std::vector<std::string> split = SplitString(_objectKey, Aws::FileSystem::PATH_DELIM);
        fileName = split.back();
        CHECKIF_MSG(fileName.empty(), E_INVALIDARG, "file name should not be empty string");
        return S_OK;
    }

    HRESULT MakeAbsolutePath(std::string& localPath)
    {
        HRESULT hr = S_OK;
        CHECKIF_MSG(localPath.empty(), E_INVALIDARG, "localPath cannot be empty string");

        // If localPath is a relative path
        if(!fs::path(localPath).is_absolute())
        {
            fs::path currentPath = fs::current_path();
            CHECKIF_MSG(currentPath.empty(), E_FAIL, "Error in getting current path");
            fs::path combinedPath = currentPath / localPath;
            localPath = LexicallyNormal(combinedPath);
        }
        return hr;
    }

    int32_t CountFiles(const Aws::String &bucketName, const Aws::String &prefix) {
        // List files request
        // default maximum number of keys is 1000
        Aws::S3::Model::ListObjectsV2Request listObjectsRequest;
        listObjectsRequest = listObjectsRequest.WithBucket(bucketName).WithPrefix(prefix);

        Aws::S3::Model::ListObjectsV2Result listObjectsResult;
        // total objects found for downloading
        int32_t count = 0;
        do 
        {
            auto listObjectsOutCome = _s3Client->ListObjectsV2(listObjectsRequest);
            if (listObjectsOutCome.IsSuccess() == false) 
            {
                TraceError("Failed to list objects from S3 Bucket");
                return 0;
            }
            listObjectsResult = listObjectsOutCome.GetResult();
            for (const auto& object : listObjectsResult.GetContents())
            {
                const Aws::String& key = object.GetKey();
                // count the number of files excluding directories
                if (!key.empty() && key[key.length() - 1] != '/')
                {
                    count++;
                }
            }
        } while (listObjectsResult.GetIsTruncated());
        return count;
    }

    ComPtr<ICredentialProvider> _cred_provider;
    ComPtr<IAwsContext> _aws_context;
    std::shared_ptr<Aws::Utils::Threading::Executor> _s3Executor;
    std::shared_ptr<Aws::Utils::Threading::Executor> _transferManagerExecutor;
    std::shared_ptr<Aws::S3::S3Client> _s3Client;
    std::shared_ptr<Aws::Transfer::TransferManager> _transferManagerClient;
    Aws::Vector<std::shared_ptr<Aws::Transfer::TransferHandle>> _directoryDownloadHandles;
    int32_t _keyCount = 0;
    std::string _s3Bucket;
    std::string _objectKey;
    std::string _region;
    std::string _localPath;
    AutoResetEvent _directoryDownloadSignalReceived;
    S3Expansion() = default;
};

DLLAPI HRESULT CreateS3Expansion(IVariableExpansion** ppObj, IStringProperty* property, ICredentialProvider* credential_provider)
{
    return S3Expansion::Create(ppObj, property, credential_provider);
}