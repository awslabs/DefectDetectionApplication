#include <Panorama/message_broker.h>
#include <Panorama/flowcontrol.h>
#include <core/message_broker/protocol_client_base.h>
#include <scheduling.h>

using namespace Panorama;

class LoopbackMessage : public ProtocolMessageBase<ILoopbackMessage>
{
public:
    static HRESULT Create(ILoopbackMessage** ppObj, IPayload* payload, const char* subscription_id)
    {
        HRESULT hr = S_OK;
        CREATE_COM(LoopbackMessage, ptr);
        CHECKHR(ptr->InitializeBase(payload));
        CHECKNULL_OR_EMPTY(subscription_id, E_INVALIDARG);
        ptr->_id = subscription_id;
        *ppObj = ptr.Detach();
        return hr;
    }

    ~LoopbackMessage()
    {
        COM_DTOR_FIN(LoopbackMessage);
    }

    const char* SubscriptionId() override
    {
        return _id.c_str();
    }

private:
    std::string _id;
};

class LoopbackSubscription : public UnknownImpl<ILoopbackSubscription>
{
public:
    static HRESULT Create(ILoopbackSubscription** ppObj, const char* subscription_id)
    {
        HRESULT hr = S_OK;
        CREATE_COM(LoopbackSubscription, ptr);
        CHECKNULL_OR_EMPTY(subscription_id, E_INVALIDARG);
        ptr->_subscription_id = subscription_id;
        *ppObj = ptr.Detach();
        return hr;
    }

    ~LoopbackSubscription()
    {
        COM_DTOR_FIN(LoopbackSubscription);
    }

    const char* SubscriptionId() override
    {
        return _subscription_id.c_str();
    }

private:
    LoopbackSubscription() = default;
    std::string _subscription_id;
};

class LoopbackProtocolClient : public ProtocolClientBase<ILoopbackSubscription>
{
public:
    static HRESULT Create(IProtocolClient** ppObj)
    {
        HRESULT hr = S_OK;
        CREATE_COM(LoopbackProtocolClient, ptr);
        CHECKHR(ptr->Initialize());
        *ppObj = ptr.Detach();
        return hr;
    }

    ~LoopbackProtocolClient()
    {
        COM_DTOR(LoopbackProtocolClient);
        _jobs.Stop();
        COM_DTOR_FIN(LoopbackProtocolClient);
    }

    HRESULT Publish(IProtocolMessage* message) override
    {
        HRESULT hr = S_OK;
        ComPtr<ILoopbackMessage> msg = ComPtr<IProtocolMessage>(message).QueryInterface<ILoopbackMessage>();
        CHECKNULL(msg, E_NOINTERFACE);

        ComPtr<IPayload> payload;
        CHECKHR(message->Payload(payload.AddressOf()));
        CHECKHR(this->InvokeOnMessageReceived(payload, [&](IProtocolSubscription* susbcription)
        {
            ComPtr<ILoopbackSubscription> instance = ComPtr<IProtocolSubscription>(susbcription).QueryInterface<ILoopbackSubscription>();
            CHECKNULL(instance, false);
            std::string msg_sub_id = msg->SubscriptionId();
            std::string inst_sub_id = instance->SubscriptionId();
            
            return strcmp(msg->SubscriptionId(), instance->SubscriptionId()) == 0;
        }));

        return hr;
    }

    HRESULT PublishAsync(IProtocolMessage* message, IProtocolClientEventHandler* eventHandler) override
    {
        ComPtr<ILoopbackMessage> msg = ComPtr<IProtocolMessage>(message).QueryInterface<ILoopbackMessage>();
        CHECKNULL(msg, E_NOINTERFACE);

        bool local_subscriber = false;

        // Check if any local subscribes to this topic
        for(auto iter = _subscriptions.begin(); iter != _subscriptions.end(); iter++)
        {
            if(strcmp(msg->SubscriptionId(), iter->second.Subscription->SubscriptionId()) == 0)
            {
                local_subscriber = true;
                break;
            }
        }

        // No local subscriber, do not continue
        // If you proceed to continue then no message received callback will be invoked but the 
        // on publish complete callback will be invoked with the loopback protocol
        // which, technically isn't wrong, just odd behavior
        if(local_subscriber == false)
        {
            return S_OK;
        }

        _jobs.Enqueue(msg, [
            msg, 
            friendly_name = _friendly_name,
            handler = ComPtr<IProtocolClientEventHandler>(eventHandler)](HRESULT hr)
        {
            if(handler != nullptr)
            {
                handler->OnMessagePublished(friendly_name.c_str(), msg, SUCCEEDED(hr));
            }
        });

        return S_OK;
    }

    HRESULT Reconnect() override 
    {
        return S_OK;
    }

    const char* FriendlyName() override
    {
        return _friendly_name.c_str();
    }

protected:
    HRESULT OnSubscription(ILoopbackSubscription* subscription) override
    {
        return S_OK;
    }

    HRESULT OnUnsubscribe(ILoopbackSubscription* subscription) override
    {
        return S_OK;
    }

private:
    LoopbackProtocolClient() = default;

    HRESULT Initialize()
    {
        _jobs.SetProcessor([&](ComPtr<ILoopbackMessage> msg)
        {
            return this->Publish(msg);
        });
        _jobs.Start();

        return S_OK;
    }

    JobQueue<ComPtr<ILoopbackMessage>> _jobs;
    std::string _friendly_name = "loopback";
};

DLLAPI HRESULT CreateLoopbackProtocolClient(IProtocolClient** ppObj)
{
    return LoopbackProtocolClient::Create(ppObj);
}

DLLAPI HRESULT CreateLoopbackMessage(ILoopbackMessage** ppObj, IPayload* payload, const char* subscription_id)
{
    return LoopbackMessage::Create(ppObj, payload, subscription_id);
}

DLLAPI HRESULT CreateLoopbackSubscription(ILoopbackSubscription** ppObj, const char* subscription_id)
{
    return LoopbackSubscription::Create(ppObj, subscription_id);
}