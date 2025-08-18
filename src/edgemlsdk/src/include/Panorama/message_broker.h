#ifndef __MESSAGE_BROKER_H__
#define __MESSAGE_BROKER_H__

#include <functional>

#include <Panorama/comptr.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/buffer.h>
#include <Panorama/credentials.h>

namespace Panorama
{
    DEF_INTERFACE(IPayload, "{A0BE4CF1-0241-4157-B7F1-4E5D35D92990}", IUnknownAlias)
    {
        /// @brief  Serializes the payload to a contiguous block of memory
        /// @param ppObj IBuffer to store the results
        /// @return S_OK on success. Error Code on failure
        virtual HRESULT Serialize(IBuffer** ppObj) = 0;

        /// @brief Serializes the payload to a contiguous block of memory.  Safe for non-null terminated data
        /// @return Pointer to the head of memory.  Nullptr if failure
        virtual const char* SerializeAsString() = 0;

        /// @brief Unique ID for this payload.  Randomly generated on payload creation
        virtual const char* Id() = 0;

        /// @brief Timestamp associated with the payload. If timestamp was not explicitly set, defaults to time of payload creation from epoch, in 10^-7 seconds (100-nanoseconds)
        virtual int64_t Timestamp() = 0;

        /// @brief Set the timestamp associated with the payload
        /// @param timestamp The timestamp to set
        /// @return S_OK on success. Error Code on failure
        virtual HRESULT SetTimestamp(int64_t timestamp) = 0;

        /// @brief Correlation ID for this payload. Empty unless set by SetCorrelationId
        virtual const char* CorrelationId() = 0;

        /// @brief Set the correlation ID for this payload
        /// @param correlationId The correlation ID to set
        /// @return S_OK on success. Error Code on failure
        virtual HRESULT SetCorrelationId(const char* correlationId) = 0;
    };

    DEF_INTERFACE(IBatchPayload, "{789123A1-40BC-4773-A246-F183D307E219}", IPayload)
    {
        /// @brief Gets the number of payloads in this batch
        virtual int32_t Count() = 0;

        /// @brief Gets a payload from the batch payload object
        /// @param ppObj IPayload to store the results
        /// @param idx Index of the payload to get
        /// @return S_OK (0) on success.  Error code otherwise
        virtual HRESULT Payload(IPayload** ppObj, int32_t idx) = 0;

        /// @brief Gets a payload from the batch payload object
        /// @param ppObj IPayload to store the results
        /// @param id Id of the payload to get
        /// @return S_OK (0) on success.  Error code otherwise
        virtual HRESULT Payload(IPayload** ppObj, const char* id) = 0;

        /// @brief Adds a payload to the batch payload object
        /// @param payload The payload to add
        /// @return S_OK (0) on success.  Error code otherwise
        virtual HRESULT AddPayload(IPayload* payload) = 0;
    };
    DLLAPI HRESULT CreateEmptyBatchPayload(IBatchPayload** ppObj);
    DLLAPI HRESULT CreatePayloadFromString(IPayload** ppObj, const char* contents);
    DLLAPI HRESULT CreatePayloadFromBuffer(IPayload** ppObj, IBuffer* contents);

    DEF_INTERFACE(IProtocolMessage, "{820312E3-4F2E-4585-8E4C-134180847184}", IUnknownAlias)
    {
        /// @brief Retrieves the payload of the message
        /// @param ppObj The IPayload object to hold the payload
        /// @return S_OK on success.  Error code on failure.
        virtual HRESULT Payload(IPayload** ppObj) = 0;

        inline ComPtr<IPayload> GetPayload()
        {
            HRESULT hr = S_OK;
            ComPtr<IPayload> payload;
            if(SUCCEEDED(this->Payload(payload.AddressOf())))
            {
                return payload;
            }

            return nullptr;
        }
    };

    DEF_INTERFACE(ILoopbackMessage, "{9145770C-E7D2-42CD-8E1C-CCE6783C7817}", IProtocolMessage)
    {
        virtual const char* SubscriptionId() = 0;
    };

    DEF_INTERFACE(IFileProtocolMessage, "{B770DC5F-66CF-456E-80B7-09F19BF419D2}", IProtocolMessage)
    {
        /// @brief Directory where the message will be saved
        virtual const char* Directory() = 0;

        /// @brief Name of the file.
        virtual const char* FileName() = 0;
    };

    DEF_INTERFACE(IGPIOProtocolMessage, "{9F9E9B8F-D6F7-4F9B-A9F6-8D7C2E6D8B3C}", IProtocolMessage)
    {
        /// @brief Rules , rule(s) for writing to GPIO
        virtual const char* Rules() = 0;

        /// @brief SignalTypes , signal_type(s) for writing to GPIO
        virtual const char* SignalTypes() = 0;

        /// @brief PulseWidthMs, pulse width(s) for waiting between writes to GPIO
        virtual const int64_t* PulseWidthMs() = 0;

        /// @brief Pins, pin for writing to GPIO
        virtual const int64_t* Pins() = 0;

        /// @brief ElemCount, number of configs of pins, rules, signal_types, pulse widths passed.
        virtual const int64_t ElemCount() = 0;
    };

    DLLAPI HRESULT CreateLoopbackMessage(ILoopbackMessage** ppObj, IPayload* payload, const char* subscription_id);

    typedef std::function<void(IPayload*)> MessageReceivedCallback;
    typedef std::function<void(const char*, IProtocolMessage*, bool)> MessagePublishedCallback;

    /// @brief Message Broker Event Handler brief
    DEF_INTERFACE(IProtocolClientEventHandler, "{84FC98E3-DDE3-4F09-AF2D-5F1DC7AFBEF7}", IUnknownAlias)
    {
        /// @brief Invoked whenever a message has been received on a subscribed topic
        /// @param data The contents of the message
        virtual void OnMessageReceived(IPayload* data) = 0;

        /// @brief Invoked whenever a message has finished its publish operation
        /// @param protocol Friendly name of the IProtocolClient that published the message
        /// @param message The published message
        /// @param successful Indicating if the message was successfully published or not
        virtual void OnMessagePublished(const char* protocol, IProtocolMessage* message, bool successful) = 0;
    };

    class ProtocolClientEventHandler : public UnknownImpl<IProtocolClientEventHandler>
    {
    public:
        static HRESULT Create(ProtocolClientEventHandler** ppObj);
        ~ProtocolClientEventHandler();

        void SetMessageReceivedCallback(MessageReceivedCallback cb);
        void SetMessagePublishedCallback(MessagePublishedCallback cb);

        void OnMessageReceived(IPayload* data) override;
        void OnMessagePublished(const char* publisher, IProtocolMessage* message, bool successful) override;

    private:
        MessageReceivedCallback _received_cb;
        MessagePublishedCallback _published_cb;
    };

    DEF_INTERFACE(IProtocolSubscription, "{BC511613-FF74-47FC-AAB5-8F7AF25B467F}", IUnknownAlias)
    {
        // Interface exists to prevent passing base IUnknownAlias*
        // to IProtocolClient::Subscribe, which is effectively
        // equivalent to passing a void*.
    };

    DEF_INTERFACE(ILoopbackSubscription, "{A01F6F46-0FA7-497E-B136-33608285CEDF}", IProtocolSubscription)
    {
        virtual const char* SubscriptionId() = 0;
    };

    DLLAPI HRESULT CreateLoopbackSubscription(ILoopbackSubscription** ppObj, const char* subscription_id);

    DEF_INTERFACE(IProtocolClient, "{B3F32887-02D0-4A8F-9E63-9AC7FFC2FB37}", IUnknownAlias)
    {
        /// @brief Subscribe to a topic
        /// @param eventHandler Event handler to invoke when a message is received on this topic
        /// @param subscription Protocol specific details for subscribing to messages
        /// @return cancellation_token that be used for unsubscribing
        virtual int32_t Subscribe(IProtocolSubscription* subscription, IProtocolClientEventHandler* eventHandler) = 0;

        /// @brief Unsubscribes from a topic
        /// @param cancellation_token The cancellation token returned from subscribe
        /// @return S_FALSE if cancellation token isn't associated to a subscription.  Error code otherwise.
        virtual HRESULT Unsubscribe(int32_t cancellation_token) = 0;

        /// @brief Synchronously publishes a message
        /// @param message The message to publish
        /// @return Error Code
        virtual HRESULT Publish(IProtocolMessage* message) = 0;

        /// @brief Asynchronously publishes a message.
        /// @param message The message to publish
        /// @param eventHandler Optional. The event handler that will be invoked when the mesage has completed publishing
        /// @return Error Code
        virtual HRESULT PublishAsync(IProtocolMessage* message, IProtocolClientEventHandler* eventHandler) = 0;

        /// @brief returns the friendly name of the protocol (e.g. 'mqtt', 's3')
        virtual const char* FriendlyName() = 0;

        /// @brief Initiates a reconnection of the IProtocolClient to its endpoint
        /// @return Error Code
        virtual HRESULT Reconnect() = 0;

        inline int32_t Subscribe(IProtocolSubscription* subscription, MessageReceivedCallback cb)
        {
            HRESULT hr = S_OK;
            ComPtr<ProtocolClientEventHandler> handler;
            CHECK_FAIL(ProtocolClientEventHandler::Create(handler.AddressOf()), E_FAIL);
            handler->SetMessageReceivedCallback(std::move(cb));
            return this->Subscribe(subscription, handler);
        }

        inline HRESULT PublishAsync(IProtocolMessage* message, MessagePublishedCallback cb=nullptr)
        {
            HRESULT hr = S_OK;
            ComPtr<ProtocolClientEventHandler> handler;
            CHECKHR(ProtocolClientEventHandler::Create(handler.AddressOf()));
            handler->SetMessagePublishedCallback(std::move(cb));
            CHECKHR(this->PublishAsync(message, handler));
            return hr;
        }
    };

    DLLAPI HRESULT CreateLoopbackProtocolClient(IProtocolClient** ppObj);

    /// @brief Interface for handling events from an IMessageBroker
    DEF_INTERFACE(IMessageBrokerEventHandler, "{44DEFBD1-8C39-4A1B-9CCF-C5443C79F1C1}", IUnknownAlias)
    {
        /// @brief Invoked when a remote message is received
        /// @param data The data received
        virtual void OnMessageReceived(IPayload* payload) = 0;

        /// @brief Called when a payload has finished publishing.
        /// @param publisher The protocol client that finished publishing
        /// @param message_id The id of the message that was published
        /// @param payload The payload that was just published
        /// @param successful Flag indicating success or failure of publishing
        virtual void OnPublished(const char* publisher, const char* message_id, IPayload* payload, bool successful) = 0;
    };

    typedef std::function<void(IPayload*)> MessageBrokerMessageReceivedCalback;
    typedef std::function<void(const char*, const char*, IPayload*, bool)> MessageBrokerMessagePublishedCallback;

    class MessageBrokerEventHandler : public UnknownImpl<IMessageBrokerEventHandler>
    {
    public:
        static HRESULT Create(MessageBrokerEventHandler** ppObj);
        void SetOnMessageReceived(MessageBrokerMessageReceivedCalback cb);
        void SetOnPublishedCompleteCallback(MessageBrokerMessagePublishedCallback cb);

        ~MessageBrokerEventHandler();
        void OnMessageReceived(IPayload* data) override;
        void OnPublished(const char* publisher, const char* message_id, IPayload* payload, bool successful) override;

    private:
        MessageBrokerMessageReceivedCalback _remote_command_cb;
        MessageBrokerMessagePublishedCallback _publish_complete_cb;
    };

    DEF_INTERFACE(IProtocolFactory, "{A6374A30-5CFD-44C8-8CAC-C79EB62C460D}", IUnknownAlias)
    {
        /// @brief Creates a protocol client from a set of JSON options
        /// @param ppObj Pointer to the created protocol client
        /// @param creation_options options as JSON
        /// @param credential_provider Provider of AWS credentials
        /// @return S_OK on success.  Error code otherwise
        virtual HRESULT CreateProtocol(IProtocolClient** ppObj, const char* creation_options, ICredentialProvider* credential_provider) = 0;

        /// @brief Validates options for creating a message are valid
        /// @param message_options The JSON message options
        /// @return S_OK if options are valid.  E_INVALIDARG otherwise.
        virtual HRESULT ValidateMessageOptions(const char* message_options) = 0;

        /// @brief Creates a protocol message from the message options
        /// @param ppObj Pointer to the created protocol message
        /// @param payload The payload of the message
        /// @param message_options Message options as JSON
        /// @return S_OK if successful.  Error code otherwise
        virtual HRESULT CreateMessage(IProtocolMessage** ppObj, IPayload* payload, const char* message_options) = 0;

        /// @brief Creates a protocol subscription from a set of JSON options
        /// @param ppObj Pointer to the created protocol subscription
        /// @param subscription_options Options for subscription as JSON
        /// @return S_OK if successful.  Error code otherwise
        virtual HRESULT CreateSubscription(IProtocolSubscription** ppObj, const char* subscription_options) = 0;

        /// @brief The name of the protocol used in the configuration file
        virtual const char* ProtocolName() = 0;
    };

    DEF_INTERFACE(IMessageBroker, "{7BF68FD8-8D46-4A7A-A1A4-F2922CEA74FE}", IUnknownAlias)
    {
        /// @brief Establishes all connections to/from the targets as defined by the pipes.  Subsequent calls result in a no-op.
        /// @return S_OK if successful.  Error code otherwise
        virtual HRESULT Initialize() = 0;

        /// @brief Synchronously publishes a payload as the source event
        /// @param message_id Name of the message
        /// @param payload The payload to publish
        /// @return S_OK (0) if successful.  Error code otherwise.
        virtual HRESULT Publish(const char* message_id, IPayload* payload) = 0;

        /// @brief Asynchronously publishes a payload as the source event
        /// @param message_id Name of the message
        /// @param payload The payload to publish
        /// @param handler Optional. Event handler for invoking OnPublishComplete
        /// @return S_OK (0) if successful.  Error code otherwise.
        virtual HRESULT PublishAsync(const char* message_id, IPayload* payload, IMessageBrokerEventHandler* handler) = 0;

        /// @brief Adds an IMessageBrokerEventHandler to the collection of handlers to handle events generated by the IEventBroker
        /// @param handler The handler to add
        /// @return Cancellation token which can be used in Unsubsribe on success. < 0 on failure.
        virtual int32_t Subscribe(const char* subscription_id, IMessageBrokerEventHandler* handler) = 0;

        /// @brief Removes an IMessageBrokerEventHandler from the collection of handlers
        /// @param cancellation_token The cancellation token returned from Subscribe
        /// @return S_OK (0) if successful.  S_FALSE if cancellation token isn't associated to a subscription.  Error code otherwise.
        virtual HRESULT Unsubscribe(int32_t cancellation_token) = 0;

        /// @brief Adds a factory for generating protocol specific message broker and message broker messages
        /// @param factory The factory to add
        /// @return S_OK (0) if successful.  Error code otherwise.
        virtual HRESULT AddProtocolFactory(IProtocolFactory* factory) = 0;

        /// @brief  Subscribe to messages received by the MessageBroker.  
        ///         It is possible for a command to be received on multiple protocols simultaneously
        ///         which could result in simultaneous invocations of the callback
        ///         It is the responsibility of the handler to ensure thread safety
        /// @param subscription_id The subscription id
        /// @param cb Method to invoke when a remote command is received
        /// @return Token that can be used for unsubscribing
        inline int32_t Subscribe(const char* subscription_id, MessageBrokerMessageReceivedCalback cb)
        {
            HRESULT hr = S_OK;
            ComPtr<MessageBrokerEventHandler> handler;
            CHECKHR(MessageBrokerEventHandler::Create(handler.AddressOf()));
            handler->SetOnMessageReceived(std::move(cb));
            return this->Subscribe(subscription_id, handler);
        }

        /// @brief Asynchronously publishes a payload as the message_id
        /// @param message_id Name of the message
        /// @param payload The payload to publish
        /// @param cb Lambda to invoke when publishing is complete
        /// @return S_OK (0) if successful.  Error code otherwise.
        inline HRESULT PublishAsync(const char* message_id, IPayload* payload, MessageBrokerMessagePublishedCallback cb=nullptr)
        {
            HRESULT hr = S_OK;
            ComPtr<MessageBrokerEventHandler> handler;
            CHECKHR(MessageBrokerEventHandler::Create(handler.AddressOf()));
            handler->SetOnPublishedCompleteCallback(std::move(cb));
            CHECKHR(this->PublishAsync(message_id, payload, handler));
            return hr;
        }

        /// @brief Asynchronously publishes a payload as the message_id
        /// @param message_id Name of the message
        /// @param contents The string to publish
        /// @param cb Lambda to invoke when publishing is complete
        /// @return S_OK (0) if successful.  Error code otherwise.
        inline HRESULT PublishAsync(const char* message_id, const char* contents, MessageBrokerMessagePublishedCallback cb=nullptr)
        {
            HRESULT hr = S_OK;
            ComPtr<IPayload> payload;
            CHECKHR(CreatePayloadFromString(payload.AddressOf(), contents));
            CHECKHR(PublishAsync(message_id, payload, std::move(cb)));
            return hr;
        }

        /// @brief Asynchronously publishes a payload as the message_id
        /// @param message_id Name of the message
        /// @param contents The buffer to publish
        /// @param cb Lambda to invoke when publishing is complete
        /// @return S_OK (0) if successful.  Error code otherwise.
        inline HRESULT PublishAsync(const char* message_id, IBuffer* contents, MessageBrokerMessagePublishedCallback cb=nullptr)
        {
            HRESULT hr = S_OK;
            ComPtr<IPayload> payload;
            CHECKHR(CreatePayloadFromBuffer(payload.AddressOf(), contents));
            CHECKHR(PublishAsync(message_id, payload, std::move(cb)));
            return hr;
        }

        /// @brief Synchronously publishes a payload as the source event
        /// @param message_id Name of the message
        /// @param contents The string to publish
        /// @return S_OK (0) if successful.  Error code otherwise.
        inline HRESULT Publish(const char* message_id, const char* contents)
        {
            HRESULT hr = S_OK;
            ComPtr<IPayload> payload;
            CHECKHR(CreatePayloadFromString(payload.AddressOf(), contents));
            CHECKHR(this->Publish(message_id, payload));
            return hr;
        }

        /// @brief Synchronously publishes a payload as the source event
        /// @param message_id Name of the message
        /// @param contents The buffer to publish
        /// @return S_OK (0) if successful.  Error code otherwise.
        inline HRESULT Publish(const char* message_id, IBuffer* contents)
        {
            HRESULT hr = S_OK;
            ComPtr<IPayload> payload;
            CHECKHR(CreatePayloadFromBuffer(payload.AddressOf(), contents));
            CHECKHR(this->Publish(message_id, payload));
            return hr;
        }
    };

    DLLAPI HRESULT CreateMessageBroker(IMessageBroker** ppObj, ICredentialProvider* credentials, const char* config, bool unique);
    DLLAPI HRESULT LoadMessageBrokerConfiguration(IBuffer** ppObj);
    DLLAPI void SetMessageBrokerDefaultConfig(const char* default_config);
    DLLAPI HRESULT CreateFileProtocolClient(IProtocolClient** ppObj);
    DLLAPI HRESULT CreateFileProtocolMessage(IFileProtocolMessage** ppObj, IPayload* payload, const char* directory, const char* filename);
    DLLAPI HRESULT CreateFileProtocolFactory(IProtocolFactory** ppObj);
    DLLAPI HRESULT CreateGPIOProtocolClient(IProtocolClient** ppObj);
    DLLAPI HRESULT CreateGPIOProtocolMessage(IGPIOProtocolMessage** ppObj, IPayload* payload, const char* rule, const char* signal_type, int64_t* pin, int64_t* pulse_width_ms, int64_t elem_count);
    DLLAPI HRESULT CreateGPIOProtocolFactory(IProtocolFactory** ppObj);

    class MessageBroker
    {
    public:
        /// @brief Creates a message broker
        /// @param ppObj Pointer to the created message broker
        /// @param credentials Provider of AWS credentials
        /// @param config configuration data.  If null (preferred) will follow prescribed method for finding configuration
        /// @param unique Will create a unique instance of the message broker, regardless of the configuration
        /// @return S_OK on success.  Error code otherwise
        static HRESULT Create(IMessageBroker** ppObj, ICredentialProvider* credentials = nullptr, const char* config = nullptr, bool unique = false)
        {
            return CreateMessageBroker(ppObj, credentials, config, unique);
        }

        /// @brief  Attempts to determine configuration for message broker.  First checks value set in by SetDefaultConfig.
        ///         If that value is null or empty then it will look in a file defined in environment variable MESSAGE_BROKER_CONFIG_FILE
        /// @param ppObj Pointer to the created buffer that holds the configuration data
        /// @return S_OK on success.  Error code otherwise
        static HRESULT LoadConfiguration(IBuffer** ppObj)
        {
            return LoadMessageBrokerConfiguration(ppObj);
        }

        /// @brief Sets the default configuration value that will be used for creating message brokers.  No validation is done here.
        /// @param config Configuration.  Null is valid, used for clearing out default config.
        static void SetDefaultConfig(const char* config)
        {
            SetMessageBrokerDefaultConfig(config);
        }

        /// @brief Creates an empty batch payload
        /// @param ppObj Pointer to the created batch payload
        /// @return S_OK on success, error code otherwise.
        static HRESULT CreateBatchPayload(IBatchPayload** ppObj)
        {
            return CreateEmptyBatchPayload(ppObj);
        }

        /// @brief Creates a payload from a string
        /// @param ppObj Pointer to the created payload
        /// @param contents The contents to contain in the payload
        /// @return S_OK on success, error code otherwise.
        static HRESULT CreatePayload(IPayload** ppObj, const char* contents)
        {
            return CreatePayloadFromString(ppObj, contents);
        }

        /// @brief Creates a payload from a buffer
        /// @param ppObj Pointer to the created payload
        /// @param contents The buffer to contain in the payload
        /// @return S_OK on success, error code otherwise.
        static HRESULT CreatePayload(IPayload** ppObj, IBuffer* contents)
        {
            return CreatePayloadFromBuffer(ppObj, contents);
        }

        /// @brief Creates the file protocol client
        /// @param ppObj Pointer to the created protocol client
        /// @return S_OK on success, error code otherwise
        static HRESULT FileProtocolClient(IProtocolClient** ppObj)
        {
            return CreateFileProtocolClient(ppObj);
        }

        /// @brief Creates a message to send to the file protocol client
        /// @param ppObj Pointer to the created message object
        /// @param payload The payload of the message
        /// @param directory The directory where the message will be saved.  Directory is created if it doesn't exist
        /// @param filename The name of the file.  Following macros are supported:
        ///                 ${id}: Inserts the id of the payload
        ///                 ${c_id}: Inserts the correlation id of the payload
        ///                 ${timestamp}: Inserts the timestamp of the payload
        ///                 Example: ${timestamp}-my_prefix-{id}
        /// @return S_OK (0) on success.  Error code otherwise
        static HRESULT FileProtocolMessage(IFileProtocolMessage** ppObj, IPayload* payload, const char* directory, const char* filename)
        {
            return CreateFileProtocolMessage(ppObj, payload, directory, filename);
        }

        /// @brief Creates the factory for the File protocol client
        /// @param ppObj Pointer to the created protocol factory
        /// @return S_OK (0) on success.  Error code otherwise
        static HRESULT FileProtocolFactory(IProtocolFactory** ppObj)
        {
            return CreateFileProtocolFactory(ppObj);
        }

        /// @brief Creates the gpio protocol client
        /// @param ppObj Pointer to the created protocol client
        /// @return S_OK on success, error code otherwise
        static HRESULT GPIOProtocolClient(IProtocolClient** ppObj)
        {
            return CreateGPIOProtocolClient(ppObj);
        }

        /// @brief Creates a message to send to the gpio protocol client
        /// @param ppObj Pointer to the created message object
        /// @param payload The payload of the message
        /// @param rule The GPIO rule
        /// @param signal_type The type of signal
        /// @param pin The pin number
        /// @param pulse_width_ms The pulse width in milliseconds
        /// @return S_OK (0) on success.  Error code otherwise
        static HRESULT GPIOProtocolMessage(IGPIOProtocolMessage** ppObj, IPayload* payload, const char* rules, const char* signal_types, int64_t* pins, int64_t* pulse_width_ms, int64_t elem_count)
        {
            return CreateGPIOProtocolMessage(ppObj, payload, rules, signal_types, pins, pulse_width_ms, elem_count);
        }

        /// @brief Creates the factory for the GPIO protocol client
        /// @param ppObj Pointer to the created protocol factory
        /// @return S_OK (0) on success.  Error code otherwise
        static HRESULT GPIOProtocolFactory(IProtocolFactory** ppObj)
        {
            return CreateGPIOProtocolFactory(ppObj);
        }
    };
}

#endif