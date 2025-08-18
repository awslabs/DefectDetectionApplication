#include <Panorama/message_broker.h>

using namespace Panorama;


HRESULT ProtocolClientEventHandler::Create(ProtocolClientEventHandler** ppObj)
{
    HRESULT hr = S_OK;
    CREATE_COM(ProtocolClientEventHandler, ptr);
    *ppObj = ptr.Detach();
    return hr;
}

ProtocolClientEventHandler::~ProtocolClientEventHandler()
{
    COM_DTOR_FIN(ProtocolClientEventHandler);
}

void ProtocolClientEventHandler::SetMessageReceivedCallback(MessageReceivedCallback cb)
{
    _received_cb = std::move(cb);
}

void ProtocolClientEventHandler::SetMessagePublishedCallback(MessagePublishedCallback cb)
{
    _published_cb = std::move(cb);
}

void ProtocolClientEventHandler::OnMessageReceived(IPayload* payload)
{
    if(_received_cb)
    {
        _received_cb(payload);
    }
}

void ProtocolClientEventHandler::OnMessagePublished(const char* publisher, IProtocolMessage* message, bool successful)
{
    if(_published_cb)
    {
        _published_cb(publisher, message, successful);
    }
}