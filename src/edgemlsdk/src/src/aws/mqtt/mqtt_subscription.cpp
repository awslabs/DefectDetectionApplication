#include <Panorama/aws.h>
#include <Panorama/message_broker.h>
#include <Panorama/flowcontrol.h>

using namespace Panorama;
class MqttSubscription : public UnknownImpl<IMqttSubscription>
{
public:
    static HRESULT Create(IMqttSubscription** ppObj, const char* topic)
    {
        HRESULT hr = S_OK;
        CREATE_COM(MqttSubscription, ptr);
        CHECKHR(ptr->Initialize(topic));
        *ppObj = ptr.Detach();
        return hr;
    }

    ~MqttSubscription()
    {
        COM_DTOR_FIN(MqttSubscription);
    }

    const char* Topic() override
    {
        return _topic.c_str();
    }

private:
    HRESULT Initialize(const char* topic)
    {
        HRESULT hr = S_OK;
        CHECKNULL(topic, E_INVALIDARG);
        CHECKIF(strlen(topic) == 0, E_INVALIDARG);

        _topic = topic;
        return hr;
    }

    std::string _topic;
};

DLLAPI HRESULT CreateMqttSubscription(IMqttSubscription** ppObj, const char* topic)
{
    return MqttSubscription::Create(ppObj, topic);
}
