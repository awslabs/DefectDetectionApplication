#ifndef __TEST_PROTOCOL_CLIENT_H__
#define __TEST_PROTOCOL_CLIENT_H__

#include <string>
#include <functional>

#include <Panorama/comptr.h>
#include <Panorama/buffer.h>
#include <Panorama/eventing.h>
#include <core/message_broker/protocol_client_base.h>

namespace Panorama
{
    DEF_INTERFACE(ITestMessage, "{4F061C1B-371A-4E80-B6B1-D46087ADAE23}", IProtocolMessage)
    {
        virtual const char* Parameter() = 0;
    };

    class TestMessage : public ProtocolMessageBase<ITestMessage>
    {
    public:
        static HRESULT Create(TestMessage** ppObj, IPayload* buffer, const char* parameter);
        ~TestMessage();
        const char* Parameter() override;

    private:
        std::string _parameter;
    };

    DEF_INTERFACE(ITestSubscription, "{C26A6766-A396-4BCF-B228-6E909904B113}", IProtocolSubscription)
    {
        virtual const char* Parameter() = 0;
    };

    class TestSubscription : public UnknownImpl<ITestSubscription>
    {
    public:
        static HRESULT Create(TestSubscription** ppObj, const char* parameter);
        ~TestSubscription();
        const char* Parameter() override;

    private:
        std::string _parameter;
    };

    typedef std::function<void(ITestMessage* message)> test_protocol_cb;

    class TestProtocolClient : public ProtocolClientBase<ITestSubscription>
    {
    public:
        static HRESULT Create(TestProtocolClient** ppObj, test_protocol_cb OnPublish);
        ~TestProtocolClient();
        HRESULT Publish(IProtocolMessage* message) override;
        HRESULT PublishAsync(IProtocolMessage* message, IProtocolClientEventHandler* eventHandler) override;
        HRESULT Reconnect() override;
        const char* FriendlyName() override;
        void InvokeSubscription(const char* id);

    protected:
        HRESULT OnSubscription(ITestSubscription* subscription) override;
        HRESULT OnUnsubscribe(ITestSubscription* subscription) override;

    private:
        test_protocol_cb _publish_cb;
        std::string _friendly_name = "test_protocol";
    };

    class TestProtocolFactory : public UnknownImpl<IProtocolFactory>
    {
    public:
        static HRESULT Create(TestProtocolFactory** ppObj, test_protocol_cb _cb);
        ~TestProtocolFactory();
        HRESULT CreateProtocol(IProtocolClient** ppObj, const char* creation_options, ICredentialProvider* credential_provider) override;
        HRESULT ValidateMessageOptions(const char* message_options) override;
        HRESULT CreateMessage(IProtocolMessage** ppObj, IPayload* payload, const char* message_options) override;
        HRESULT CreateSubscription(IProtocolSubscription** ppObj,const char* subscription_options) override;
        const char* ProtocolName() override;
        void InvokeSubscription(const char* id);

    private:
        test_protocol_cb _cb;
        ComPtr<TestProtocolClient> _created_client;
    };
}

#endif