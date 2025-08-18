#include <fstream>

#include <Panorama/message_broker.h>
#include <core/message_broker/protocol_client_base.h>
#include <scheduling.h>
#include <filesystem_safe.h>

using namespace Panorama;

class FileProtocolClient : public ProtocolClientBase<IProtocolSubscription>
{
public:
    static HRESULT Create(IProtocolClient** ppObj)
    {
        COM_FACTORY(FileProtocolClient, Initialize());
    }

    ~FileProtocolClient()
    {
        COM_DTOR(FileProtocolClient);
        _save_job.Stop();
        COM_DTOR_FIN(FileProtocolClient);
    }

    HRESULT Publish(IProtocolMessage* message) override
    {
        HRESULT hr = S_OK;
        ComPtr<IFileProtocolMessage> msg = ComPtr<IProtocolMessage>(message).QueryInterface<IFileProtocolMessage>();
        CHECKIF(msg == nullptr, E_NOINTERFACE);
        CHECKHR(this->SaveFile(msg));
        return hr;
    }

    HRESULT PublishAsync(IProtocolMessage* message, IProtocolClientEventHandler* eventHandler) override
    {
        HRESULT hr = S_OK;
        ComPtr<IFileProtocolMessage> msg = ComPtr<IProtocolMessage>(message).QueryInterface<IFileProtocolMessage>();
        CHECKIF(msg == nullptr, E_NOINTERFACE);
        _save_job.Enqueue(msg, [
            msg, 
            name = _friendly_name,
            handler = ComPtr<IProtocolClientEventHandler>(eventHandler)](HRESULT res)
        {
            if(handler)
            {
                handler->OnMessagePublished(name.c_str(), msg, SUCCEEDED(res));
            }
        });

        return hr;
    }

    const char* FriendlyName() override
    {
        return _friendly_name.c_str();
    }

    HRESULT Reconnect() override
    {
        return S_OK;
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
    FileProtocolClient() = default;

    HRESULT Initialize()
    {
        _save_job.SetProcessor([&](ComPtr<IFileProtocolMessage> message)
        {
            return this->SaveFile(message);
        });
        _save_job.SetName("FileProtocolClientJobQueue");
        _save_job.Start();
        return S_OK;
    }

    HRESULT SaveFile(ComPtr<IFileProtocolMessage> message)
    {
        HRESULT hr = S_OK;

        // Check the directory exists
        fs::path dirPath(message->Directory());

        // Check if the directory exists
        if (fs::exists(dirPath) == false) 
        {
            // Directory does not exist, so create it
            CHECKIF_MSG(fs::create_directories(dirPath) == false, E_FAIL, "Could not create directory %s", message->Directory());
        }

        // Get the buffer for the data
        ComPtr<IPayload> payload;
        CHECKHR(message->Payload(payload.AddressOf()));

        ComPtr<IBuffer> buffer;
        CHECKHR(payload->Serialize(buffer.AddressOf()));

        // Write the file to disk
        fs::path full_path = dirPath / fs::path(message->FileName());
        std::ofstream file(full_path.c_str(), std::ios::out | std::ios::binary);
        CHECKIF_MSG(file.is_open() == false, E_FAIL, "Could not open %s for writing", full_path.c_str());

        try
        {
            file.write(reinterpret_cast<const char*>(buffer->Data()), buffer->Size());
            file.flush();
        }
        catch(const std::exception& e)
        {
            TraceError("Exception raise during saving of payload: %s", e.what());
            return E_FAIL;
        }

        try
        {
            file.close();
        }
        catch(const std::exception& e)
        {
            TraceWarning("Exception raised during file close: %s", e.what());
        }

        return S_OK;
    }

    MultiThreadedJobQueue<ComPtr<IFileProtocolMessage>> _save_job;
    std::string _friendly_name = "file";
};

DLLAPI HRESULT CreateFileProtocolClient(IProtocolClient** ppObj)
{
    return FileProtocolClient::Create(ppObj);
}