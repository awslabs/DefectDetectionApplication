#include <Panorama/message_broker.h>

using namespace Panorama;


HRESULT MessageBrokerEventHandler::Create(MessageBrokerEventHandler** ppObj)
{
    HRESULT hr = S_OK;
    CREATE_COM(MessageBrokerEventHandler, ptr);
    *ppObj = ptr.Detach();
    return hr;
}

MessageBrokerEventHandler::~MessageBrokerEventHandler()
{
    COM_DTOR_FIN(EventBrokerEventHandler);
}

void MessageBrokerEventHandler::SetOnMessageReceived(MessageBrokerMessageReceivedCalback cb)
{
    _remote_command_cb = std::move(cb);
}

void MessageBrokerEventHandler::SetOnPublishedCompleteCallback(MessageBrokerMessagePublishedCallback cb)
{
    _publish_complete_cb = std::move(cb);
}

void MessageBrokerEventHandler::OnMessageReceived(IPayload* buffer)
{
    if(_remote_command_cb != nullptr)
    {
        _remote_command_cb(buffer);
    }
}

void MessageBrokerEventHandler::OnPublished(const char* publisher, const char* message_id, IPayload* payload, bool successful)
{
    if(_publish_complete_cb != nullptr)
    {
        _publish_complete_cb(publisher, message_id, payload, successful);
    }
}