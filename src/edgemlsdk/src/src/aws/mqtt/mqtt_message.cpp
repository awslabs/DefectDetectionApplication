#include <Panorama/aws.h>
#include <Panorama/message_broker.h>
#include <Panorama/flowcontrol.h>
#include <core/message_broker/protocol_client_base.h>

using namespace Panorama;

class MqttMessage : public ProtocolMessageBase<IMqttMessage>
{
public:
    static HRESULT Create(IMqttMessage** ppObj, IPayload* payload, const char* topic)
    {
        HRESULT hr = S_OK;
        CREATE_COM(MqttMessage, ptr);
        CHECKHR(ptr->Initialize(payload, topic));
        *ppObj = ptr.Detach();
        return hr;
    }

    const char* Topic() override
    {
        return _topic.c_str();
    }

    ~MqttMessage()
    {
        COM_DTOR_FIN(MqttMessage);
    }

private:
    HRESULT Initialize(IPayload* payload, const char* topic)
    {
        HRESULT hr = S_OK;
        CHECKHR(InitializeBase(payload));
        CHECKNULL(topic, E_INVALIDARG);
        CHECKIF(strlen(topic) == 0, E_INVALIDARG);

        _topic = topic;
        return hr;
    }

    std::string _topic;
};

DLLAPI HRESULT CreateMqttMessage(IMqttMessage** ppObj, IPayload* payload, const char* topic)
{
    return MqttMessage::Create(ppObj, payload, topic);
}
