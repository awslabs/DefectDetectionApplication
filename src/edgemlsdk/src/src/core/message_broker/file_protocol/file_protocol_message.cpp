#include <Panorama/message_broker.h>
#include <Panorama/flowcontrol.h>
#include <core/message_broker/protocol_client_base.h>
#include <filesystem_safe.h>

using namespace Panorama;

class FileMessage : public ProtocolMessageBase<IFileProtocolMessage>
{
public:
    static HRESULT Create(IFileProtocolMessage** ppObj, IPayload* payload, const char* directory, const char* file_name)
    {
        COM_FACTORY(FileMessage, Initialize(payload, directory, file_name));
    }

    ~FileMessage()
    {
        COM_DTOR_FIN(FileMessage);
    }

    const char* Directory() override
    {
        return _directory.c_str();
    }

    const char* FileName() override
    {
        return _filename.c_str();
    }

private:
    FileMessage() = default;

    HRESULT Initialize(IPayload* payload, const char* directory, const char* file_name)
    {
        HRESULT hr = S_OK;
        CHECKHR(InitializeBase(payload));
        CHECKNULL_OR_EMPTY(directory, E_INVALIDARG);
        CHECKNULL_OR_EMPTY(file_name, E_INVALIDARG);

        _directory = directory;
        _filename = ExpandMacros(file_name, payload);
        CHECKIF_MSG(_filename.length() == 0, E_INVALIDARG, "Macro expansion of file name failed");

        return hr;
    }

    std::string _directory;
    std::string _filename;
}; 

DLLAPI HRESULT CreateFileProtocolMessage(IFileProtocolMessage** ppObj, IPayload* payload, const char* directory, const char* filename)
{
    return FileMessage::Create(ppObj, payload, directory, filename);
}