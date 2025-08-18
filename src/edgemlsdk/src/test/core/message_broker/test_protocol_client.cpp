#include <nlohmann/json.hpp>
#include <misc.h>
#include <thread>
#include "test_protocol_client.h"

using namespace Panorama;

// ====== Message ======
HRESULT TestMessage::Create(TestMessage** ppObj, IPayload* payload, const char* parameter)
{
    HRESULT hr = S_OK;
    CREATE_COM(TestMessage, ptr);
    CHECKHR(ptr->InitializeBase(payload));
    CHECKNULL_OR_EMPTY(parameter, E_INVALIDARG);
    ptr->_parameter = parameter;
    *ppObj = ptr.Detach();
    return hr;
}

TestMessage::~TestMessage()
{
    COM_DTOR_FIN(TestMessage);
}

const char* TestMessage::Parameter()
{
    return _parameter.c_str();
}

// ======= Subscription ======
HRESULT TestSubscription::Create(TestSubscription** ppObj, const char* parameter)
{
    HRESULT hr = S_OK;
    CREATE_COM(TestSubscription, ptr);
    CHECKNULL_OR_EMPTY(parameter, E_INVALIDARG);
    ptr->_parameter = parameter;
    *ppObj = ptr.Detach();
    return hr;
}

TestSubscription::~TestSubscription()
{
    COM_DTOR_FIN(TestSubscription);
}

const char* TestSubscription::Parameter()
{
    return _parameter.c_str();
}

// ====== Protocol Client =======
HRESULT TestProtocolClient::Create(TestProtocolClient** ppObj, test_protocol_cb OnPublish)
{
    HRESULT hr = S_OK;
    CREATE_COM(TestProtocolClient, ptr);
    ptr->_publish_cb = std::move(OnPublish);
    *ppObj = ptr.Detach();
    return hr;
}

TestProtocolClient::~TestProtocolClient()
{
    COM_DTOR_FIN(TestProtocolClient);
}   

HRESULT TestProtocolClient::Publish(IProtocolMessage* message)
{
    ComPtr<ITestMessage> msg = ComPtr<IProtocolMessage>(message).QueryInterface<ITestMessage>();
    CHECKNULL(msg, E_NOINTERFACE);

    if(_publish_cb != nullptr)
    {
        _publish_cb(msg);
    }

    return S_OK;
}

HRESULT TestProtocolClient::PublishAsync(IProtocolMessage* message, IProtocolClientEventHandler* eventHandler)
{
    ComPtr<ITestMessage> msg = ComPtr<IProtocolMessage>(message).QueryInterface<ITestMessage>();
    CHECKNULL(msg, E_NOINTERFACE);

    std::thread t = std::thread([
        msg,
        cb = _publish_cb,
        friendly_name = _friendly_name,
        handler = ComPtr<IProtocolClientEventHandler>(eventHandler)
    ]()
    {
        if(cb != nullptr)
        {
            cb(msg);
        }

        if(handler != nullptr)
        {
            handler->OnMessagePublished(friendly_name.c_str(), msg, true);
        }
    });
    t.detach();

    return S_OK;
}

HRESULT TestProtocolClient::Reconnect()
{
    return S_OK;
}

const char* TestProtocolClient::FriendlyName()
{
    return _friendly_name.c_str();
}

HRESULT TestProtocolClient::OnSubscription(ITestSubscription* subscription)
{
    return S_OK;
}

HRESULT TestProtocolClient::OnUnsubscribe(ITestSubscription* subscription)
{
    return S_OK;
}

void TestProtocolClient::InvokeSubscription(const char* id)
{
    ComPtr<IPayload> payload;
    MessageBroker::CreatePayload(payload.AddressOf(), id);
    this->InvokeOnMessageReceived(payload, [&](ITestSubscription* subscription)
    {
        return strcmp(subscription->Parameter(), id) == 0;
    });
}

// ======= Protocol Factory ========
HRESULT TestProtocolFactory::Create(TestProtocolFactory** ppObj, test_protocol_cb cb)
{
    HRESULT hr = S_OK;
    CREATE_COM(TestProtocolFactory, ptr);
    ptr->_cb = std::move(cb);
    *ppObj = ptr.Detach();
    return hr;
}

TestProtocolFactory::~TestProtocolFactory()
{
    COM_DTOR_FIN(TestProtocolFactory);
}

HRESULT TestProtocolFactory::CreateProtocol(IProtocolClient** ppObj, const char* creation_options, ICredentialProvider* credential_provider)
{
    HRESULT hr = S_OK;
    CHECKHR(TestProtocolClient::Create(_created_client.AddressOf(), _cb));
    _created_client.AddRef();
    *ppObj = _created_client.Ptr();
    return hr;
}

HRESULT TestProtocolFactory::ValidateMessageOptions(const char* message_options)
{
    nlohmann::json jObj = nlohmann::json::parse(message_options);

    CHECKIF(ValidateJsonProperty<const char*>(jObj, "parameter") == false, E_INVALIDARG);
    return S_OK;
}

HRESULT TestProtocolFactory::CreateMessage(IProtocolMessage** ppObj, IPayload* payload, const char* message_options)
{
    HRESULT hr = S_OK;
    CHECKHR(ValidateMessageOptions(message_options));
    nlohmann::json jObj = nlohmann::json::parse(message_options);

    std::string parameter1 = jObj["parameter"];

    ComPtr<TestMessage> msg;
    TestMessage::Create(msg.AddressOf(), payload, parameter1.c_str());
    *ppObj = msg.Detach();
    return hr;
}

HRESULT TestProtocolFactory::CreateSubscription(IProtocolSubscription** ppObj, const char* subscription_options)
{
    HRESULT hr = S_OK;
    nlohmann::json jObj = nlohmann::json::parse(subscription_options);
    std::string parameter = jObj["parameter"];

    ComPtr<TestSubscription> sub;
    CHECKHR(TestSubscription::Create(sub.AddressOf(), parameter.c_str()));
    *ppObj = sub.Detach();
    return hr;
}

const char* TestProtocolFactory::ProtocolName()
{
    static std::string name = "test_protocol";
    return name.c_str();
}

void TestProtocolFactory::InvokeSubscription(const char* id)
{
    _created_client->InvokeSubscription(id);
}