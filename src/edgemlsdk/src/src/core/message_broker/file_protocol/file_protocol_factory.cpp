#include <nlohmann/json.hpp>

#include <Panorama/message_broker.h>
#include <Panorama/flowcontrol.h>
#include <misc.h>

using namespace Panorama;

class FileProtocolFactory : public UnknownImpl<IProtocolFactory>
{
public:
    static HRESULT Create(IProtocolFactory** ppObj)
    {
        COM_FACTORY(FileProtocolFactory, Initialize());
    }

    ~FileProtocolFactory()
    {
        COM_DTOR_FIN(FileProtocolFactory);
    }

    HRESULT CreateProtocol(IProtocolClient** ppObj, const char* creation_options, ICredentialProvider* credential_provider) override
    {
        return MessageBroker::FileProtocolClient(ppObj);
    }

    HRESULT ValidateMessageOptions(const char* message_options) override
    {
        CHECKIF_MSG(nlohmann::json::accept(message_options) == false, E_INVALIDARG, "Could not parse message options for file protocol");
        nlohmann::json jObj = nlohmann::json::parse(message_options);

        CHECKIF_MSG(ValidateJsonProperty<const char*>(jObj, "directory", true) == false, E_INVALIDARG, "Parameter 'directory' is mising or not a string in file protocol message options");
        CHECKIF_MSG(ValidateJsonProperty<const char*>(jObj, "filename", true) == false, E_INVALIDARG, "Parameter 'filename' is mising or not a string in file protocol message options");
        CHECKIF_MSG(ValidateJsonProperty<const char*>(jObj, "extension", false) == false, E_INVALIDARG, "Parameter 'extension' is not a string in file protocol message options");

        return S_OK;
    }

    HRESULT CreateMessage(IProtocolMessage** ppObj, IPayload* payload, const char* message_options) override
    {
        HRESULT hr = S_OK;
        CHECKHR(ValidateMessageOptions(message_options));
        nlohmann::json jObj = nlohmann::json::parse(message_options);

        std::string directory = jObj["directory"];
        std::string filename = jObj["filename"];

        ComPtr<IFileProtocolMessage> msg;
        CHECKHR(MessageBroker::FileProtocolMessage(msg.AddressOf(), payload, directory.c_str(), filename.c_str()));

        *ppObj = msg.Detach();
        return hr;
    }

    HRESULT CreateSubscription(IProtocolSubscription** ppObj, const char* subscription_options) override
    {
        return E_NOTIMPL;
    }

    const char* ProtocolName() override
    {
        return "file";
    }

private:
    HRESULT Initialize()
    {
        return S_OK;
    }
};

DLLAPI HRESULT CreateFileProtocolFactory(IProtocolFactory** ppObj)
{
    return FileProtocolFactory::Create(ppObj);
}