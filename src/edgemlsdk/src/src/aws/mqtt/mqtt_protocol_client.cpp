#include <queue>

#include <aws/core/Aws.h>
#include <aws/crt/auth/Credentials.h>
#include <aws/crt/Api.h>
#include <aws/crt/UUID.h>
#include <aws/crt/Types.h>
#include <aws/crt/Allocator.h>
#include <aws/crt/JsonObject.h>
#include <aws/crt/Exports.h>
#include <aws/iot/MqttClient.h>

#include <nlohmann/json.hpp>

#include <Panorama/apidefs.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/credentials.h>
#include <Panorama/message_broker.h>
#include <Panorama/eventing.h>
#include <Panorama/aws.h>
#include<chrono>
#include <scheduling.h>
#include <misc.h>
#include <core/message_broker/protocol_client_base.h>

using namespace Panorama;
using namespace Aws::Crt;
using namespace Aws::Crt::Mqtt;
constexpr int MQTT_RECONNECTION_LIMIT_HRS = 12;
constexpr int ACK_WAIT_TIME_MS = 5000;
constexpr float DEFAULT_BACKOFF = 0.25f;

struct PublishContext
{
    std::string Topic;
    ComPtr<IBuffer> Payload;
};

class MQTTMessageBroker : public ProtocolClientBase<IMqttSubscription>
{
public:
    static HRESULT Create(IProtocolClient** ppObj, const char* endpoint, const char* region, ICredentialProvider* credential_provider, const char* client_id)
    {
        HRESULT hr = S_OK;
        CREATE_COM(MQTTMessageBroker, ptr);
        CHECKHR(ptr->Initialize(endpoint, region, credential_provider, client_id));
        *ppObj = ptr.Detach();

        return hr;
    }

    ~MQTTMessageBroker()
    {
        COM_DTOR(MQTTMessageBroker);

        _publishJobs.Stop();

        // Disconnect from mqtt_connection.
        // Failure to do so will cause Aws::Shutdown to hang
        if(_mqtt_connection != nullptr)
        {
            if(_mqtt_connection->Disconnect())
            {
                TraceVerbose("Disconnecting");
                _disconnecting.Wait();
            }
        }

        // Forcing this to be destroyed before AwsContext is destroyed
        // If AwsContext goes out of scope first and that was the only reference
        // than Aws SDK will crash when trying to delete aws resources....0 out of 10!
        _mqtt_client.reset();
        _mqtt_connection.reset();
        _shutdown = true;
        _reconnect.Set();
        if(_reconnect_thread.joinable())
        {
            _reconnect_thread.join();
        } 
        
        COM_DTOR_FIN(MQTTMessageBroker);
    }

    HRESULT Publish(IProtocolMessage* message) override
    {
        HRESULT hr = S_OK;
        CHECKNULL(message, E_INVALIDARG);
        ComPtr<IMqttMessage> msg = ComPtr<IProtocolMessage>(message).QueryInterface<IMqttMessage>();
        CHECKNULL(msg, E_NOINTERFACE);

        CHECKHR(this->PublishInternal(msg));
        return hr;
    }

    HRESULT PublishAsync(IProtocolMessage* message, IProtocolClientEventHandler* eventHandler) override
    {
        CHECKNULL(message, E_INVALIDARG);
        ComPtr<IMqttMessage> msg = ComPtr<IProtocolMessage>(message).QueryInterface<IMqttMessage>();
        CHECKNULL(msg, E_NOINTERFACE);

        this->_publishJobs.Enqueue(msg, [
            msg,
            friendly_name = _friendly_name,
            handler = ComPtr<IProtocolClientEventHandler>(eventHandler)](HRESULT res)
        {
            if(handler)
            {
                handler->OnMessagePublished(friendly_name.c_str(), msg, SUCCEEDED(res));
            }
        });

        return S_OK;
    }

    HRESULT Reconnect() override 
    {
        HRESULT hr = S_OK;
        mqtt_reconnect();
        return hr;
    }

protected:
    HRESULT OnSubscription(IMqttSubscription* subscription) override
    {
        HRESULT hr = S_OK;
        CHECKNULL(subscription, E_INVALIDARG);
        CHECKHR(SubscribeInternal(subscription->Topic()));
        return hr;
    }

    HRESULT OnUnsubscribe(IMqttSubscription* subscription) override
    {
        HRESULT hr = S_OK;
        CHECKNULL(subscription, E_INVALIDARG);
        CHECKHR(UnsubscribeInternal(subscription->Topic()));
        return hr;
    }

    const char* FriendlyName() override
    {
        return _friendly_name.c_str();
    }

private:
    HRESULT Initialize(const char* endpoint, const char* region, ICredentialProvider* credential_provider, const char* client_id)
    {
        HRESULT hr = S_OK;
        CHECKNULL(endpoint, E_INVALIDARG);
        CHECKNULL(region, E_INVALIDARG);
        CHECKNULL(credential_provider, E_INVALIDARG);
        CHECKHR(AwsContext(_awsContext.AddressOf()));

        _region = region;
        _iot_endpoint = endpoint;

        // If client id isn't specified, generate a random UUID
        if(client_id == nullptr || strlen(client_id) == 0)
        {
            _client_id = Aws::Crt::UUID().ToString();
        }
        else
        {
            _client_id = client_id;
        }
        
        _credentialProvider = credential_provider;
        TraceInfo("Creating MQTT Protocol Client: Endpoint=%s, Region=%s, Client-ID=%s", _iot_endpoint.c_str(), _region.c_str(), _client_id.c_str());

        auto delegateGetCredentials = [this]() -> std::shared_ptr<Aws::Crt::Auth::Credentials> {
            auto aws_credentials = _credentialProvider->GetAWSCredentials();

            Aws::Crt::Auth::Credentials credentials(
                aws_byte_cursor_from_c_str(aws_credentials.GetAWSAccessKeyId().c_str()),
                aws_byte_cursor_from_c_str(aws_credentials.GetAWSSecretKey().c_str()),
                aws_byte_cursor_from_c_str(aws_credentials.GetSessionToken().c_str()),
                aws_credentials.GetExpiration().Seconds());

            return Aws::Crt::MakeShared<Aws::Crt::Auth::Credentials>(
                Aws::Crt::ApiAllocator(), credentials.GetUnderlyingHandle());
        };

        Aws::Crt::Auth::CredentialsProviderDelegateConfig config;
        config.Handler = delegateGetCredentials;
        _icredentials_provider = Aws::Crt::Auth::CredentialsProvider::CreateCredentialsProviderDelegate(config);
        build_mqtt_connection();

        _publishJobs.SetProcessor([&](ComPtr<IMqttMessage> context)
        {
            return this->PublishInternal(context);
        });

        _publishJobs.Start();
        _reconnect_thread = std::thread([&]()
        {
            while(_shutdown == false) 
            {
                _reconnect.WaitFor(MQTT_RECONNECTION_LIMIT_HRS*60*60*1000);
                if(_shutdown)
                {
                    break;
                }

                mqtt_reconnect();
            }
        });
                
        return mqtt_connect();
    }

    HRESULT build_mqtt_connection()
    {
        HRESULT hr = S_OK;
        _mqtt_client = std::make_shared<Aws::Iot::MqttClient>(Aws::Iot::MqttClient());
        CHECKNULL_MSG(_mqtt_client, E_FAIL, "Unable to create mqtt client");
        Aws::Iot::WebsocketConfig ws_config(_region.c_str(), _icredentials_provider);
        Aws::Iot::MqttClientConnectionConfigBuilder client_config_builder;
        client_config_builder = Aws::Iot::MqttClientConnectionConfigBuilder(ws_config);
        client_config_builder.WithEndpoint(_iot_endpoint.c_str());
        Aws::Iot::MqttClientConnectionConfig client_config = client_config_builder.Build();

        _mqtt_connection = _mqtt_client->NewConnection(client_config);
        CHECKNULL_MSG(_mqtt_connection, E_FAIL, "Failed to create the mqtt connection");

        // mqtt life cycle
        _mqtt_connection->OnConnectionCompleted =
            [&](Aws::Crt::Mqtt::MqttConnection& connection, int errorCode,
                Aws::Crt::Mqtt::ReturnCode returnCode, bool sessionPresent) 
            {
                _connected = errorCode == 0;
                if (errorCode)
                {
                    TraceError("Connection to endpoint %s failed with error %s", _iot_endpoint.c_str(), Aws::Crt::ErrorDebugString(errorCode));
                }
                else
                {
                    TraceInfo("Connection to endpoint %s complete", _iot_endpoint.c_str());
                    _backoff = DEFAULT_BACKOFF;
                }

                _connecting.Set();
            };

        _mqtt_connection->OnConnectionResumed = 
            [](Aws::Crt::Mqtt::MqttConnection& connection,
                Aws::Crt::Mqtt::ReturnCode connectCode,
                bool sessionPresent) 
            {
                    TraceInfo("Connect has resumed with return code - %d", static_cast<int32_t>(connectCode));
            };

        _mqtt_connection->OnConnectionInterrupted = 
            [this](Aws::Crt::Mqtt::MqttConnection& connection, int error)
            {
                TraceInfo("Connection was interrupted with error - %s", Aws::Crt::ErrorDebugString(error));
                if(error == AWS_ERROR_MQTT_UNEXPECTED_HANGUP) 
                {
                    _reconnect.Set();
                }
            };

        _mqtt_connection->OnDisconnect = 
            [&](Aws::Crt::Mqtt::MqttConnection& connection)
            {
                TraceInfo("Disconnection complete");
                _connected = false;
                _disconnecting.Set();
            };

        return hr;
    }

    HRESULT mqtt_connect() 
    {
        if (_mqtt_connection->SetReconnectTimeout(1, 1024) == false) 
        {
            TraceError("Failed to set mqtt reconnection timeout with error - %s", Aws::Crt::ErrorDebugString(_mqtt_connection->LastError()));
        }

        CHECKIF_MSG(_mqtt_connection->Connect(_client_id.c_str(), false) == false, E_FAIL, "Failed to attempt mqtt connection");

        // Wait for connection process to complete
        _connecting.Wait();
        return _connected ? S_OK : E_FAIL;
    }

    void mqtt_reconnect()
    {
        HRESULT hr = S_OK;
        std::unique_lock lock(_mtx);
        _publishJobs.Pause();

        // Disconnect from mqtt_connection.
        // Failure to do so will cause Aws::Shutdown to hang
        if(_mqtt_connection != nullptr)
        {
            TraceInfo("Calling Disconnect in MQTT Reconnect");
            if(_mqtt_connection->Disconnect())
            {
                TraceVerbose("Disconnecting");
                _disconnecting.Wait();
            }
        }

        // Can't have 2 clients connected with the same client ID
        // Even though we have disconnected on our end there is undetermined amount of 
        // time before the server cleans up it's resources.
        // Introduce a small delay that exponentially increases so we don't get into a tight loop of connection interuppted (UNEXPECTED_HANGUP) reconnect.
        // _backoff gets reset to default on successful connection
        TraceVerbose("Delay before reconnect: %f seconds", _backoff);
        ThreadSleep(_backoff * 1000.0f); 
        _backoff = std::min(2.0f * _backoff, 16.0f);

        _mqtt_client.reset();
        _mqtt_connection.reset();

        CHECK_FAIL_MSG(build_mqtt_connection(),,"Failed to build mqtt connection");
        CHECK_FAIL_MSG(mqtt_connect(),,"Faled to connect");

        std::map<std::string, int32_t> original_subscriptions = _sub_count;
        _sub_count.clear();

        _publishJobs.Resume();

        lock.unlock();

        for(const auto& pair: original_subscriptions) 
        {
            SubscribeInternal(pair.first);
        }
    }

    HRESULT PublishInternal(ComPtr<IMqttMessage> context)
    {
        HRESULT hr = S_OK;

        std::lock_guard<std::mutex> guard(_mtx);
        ComPtr<IPayload> payload = context->GetPayload();
        CHECKNULL(payload, E_INVALIDARG);

        ComPtr<IBuffer> serialized;
        CHECKHR(payload->Serialize(serialized.AddressOf()));

        ByteBuf byte_payload = Aws::Crt::ByteBufFromArray(serialized->Data(), serialized->Size());
        ManualResetEvent publishComplete;
        uint16_t packetId = _mqtt_connection->Publish(context->Topic(), AWS_MQTT_QOS_AT_LEAST_ONCE, false, byte_payload, 
                            [&](MqttConnection&, uint16_t packetId, int errorCode)
                            {
                                if (errorCode) 
                                {
                                    TraceError("Failed to publish packet %u - %s", packetId, Aws::Crt::ErrorDebugString(errorCode));
                                } 
                                else 
                                {
                                    if (packetId == 0) 
                                    {
                                        TraceError("publish rejected by the mqtt broker");
                                    } 
                                    else 
                                    {
                                        TraceVerbose("Packet %u published on topic %s", packetId, context->Topic());
                                    }
                                }

                                publishComplete.Set();
                            });

        if(packetId <= 0)
        {
            return E_FAIL;
        }

        publishComplete.Wait();
        return S_OK;
    }

    HRESULT SubscribeInternal(std::string sub_topic)
    {
        std::lock_guard<std::mutex> guard(_mtx);

        // Check if underlying mqtt_connection has subscribed to this topic
        // increment number of subscribes by one in the event that we have
        if(_sub_count.find(sub_topic) != _sub_count.end())
        {
            _sub_count[sub_topic]++;
            return S_OK;
        }

        // Not yet subscribed to topic, have underlying mqtt connection subscribe to topic
        uint16_t packetId = _mqtt_connection->Subscribe(sub_topic.c_str(), AWS_MQTT_QOS_AT_LEAST_ONCE, 
            [&](MqttConnection &connection, const Aws::Crt::String &topic, const Aws::Crt::ByteBuf &payload, bool dup, QOS qos, bool retain)
            {
                TraceVerbose("Received message on topic %s", topic.c_str());
                // Create IBuffer from Aws::Crt::ByteBuf
                HRESULT hr;
                ComPtr<IBuffer> buf;
                hr = Buffer::Create(buf.AddressOf(), payload.len);
                if(FAILED(hr))
                {
                    TraceError("Failed to create buffer from Aws::Crt::ByteBuf %s", ErrorCodeToString(hr));
                    return;
                }

                memcpy(buf->Data(), payload.buffer, buf->Size());

                ComPtr<IPayload> received_payload;
                MessageBroker::CreatePayload(received_payload.AddressOf(), buf);

                // Invoke OnMessageReceived on all appropriate event handlers
                this->InvokeOnMessageReceived(received_payload, [&](IProtocolSubscription* subscription)
                {
                    ComPtr<IMqttSubscription> mqtt_subscription = ComPtr<IProtocolSubscription>(subscription).QueryInterface<IMqttSubscription>();
                    CHECKNULL(mqtt_subscription, false);

                    // This cast is as all subscriptions added to base go through OnSubscription method where upcast is check
                    return strcmp(mqtt_subscription->Topic(), topic.c_str()) == false;
                });
            }, 
            [&](MqttConnection &connection, uint16_t packetId, const Aws::Crt::String &topic, QOS qos, int errorCode)
            {
                // Message Suback
            });

        CHECKIF_MSG(packetId == 0, E_FAIL, "Failed to subscribe to the requested topic");
        TraceInfo("Subscribed to topic %s", sub_topic.c_str());
        _sub_count[sub_topic] = 1;
        return S_OK;
    }

    HRESULT UnsubscribeInternal(std::string sub_topic)
    {
        std::lock_guard<std::mutex> guard(_mtx);

        // Check if any callbacks associated with this topic
        if(_sub_count.find(sub_topic) == _sub_count.end())
        {
            return S_FALSE;
        }

        // We are subscribed, decrement number of callbacks associated to this topic
        _sub_count[sub_topic]--;
        if(_sub_count[sub_topic] > 0)
        {
            return S_OK;
        }

        TraceVerbose("Unsubscribing from %s", sub_topic.c_str());
        // No callbacks for this topic, unsubcribe from underlying mqtt_connection
        ManualResetEvent unsubscribeComplete;
        _mqtt_connection->Unsubscribe(sub_topic.c_str(), [sub_topic, &unsubscribeComplete](MqttConnection &connection, uint16_t packetId, int errorCode)
        {
            if (errorCode)
            {
                TraceError("Failed to unsubscribe from %s: %s", sub_topic.c_str(), Aws::Crt::ErrorDebugString(errorCode));
            }
            else
            {
                TraceInfo("Unsubscribed from %s", sub_topic.c_str());
            }

            unsubscribeComplete.Set();
        });

        CHECKIF_MSG(unsubscribeComplete.WaitFor(ACK_WAIT_TIME_MS) == false, E_TIMEOUT, "Did not receive ack message for unsubscribe");
        _sub_count.erase(sub_topic);
        return S_OK;
    }

    Aws::String _region;
    Aws::String _iot_endpoint;
    Aws::String _client_id;
    std::shared_ptr<Aws::Iot::MqttClient> _mqtt_client;
    std::shared_ptr<MqttConnection> _mqtt_connection;
    ComPtr<ICredentialProvider> _credentialProvider;
    ComPtr<IAwsContext> _awsContext;
    AutoResetEvent _connecting;
    AutoResetEvent _disconnecting;
    bool _connected = false;
    std::mutex _mtx;
    JobQueue<ComPtr<IMqttMessage>> _publishJobs;
    std::thread _reconnect_thread;
    ManualResetEvent _reconnect;
    bool _shutdown = false;
    std::shared_ptr<Aws::Crt::Auth::ICredentialsProvider> _icredentials_provider;
    std::map<std::string, int32_t> _sub_count;
    std::string _friendly_name = "mqtt";
    float _backoff = DEFAULT_BACKOFF;
};

DLLAPI HRESULT CreateMQTTProtocolClient(IProtocolClient** ppObj, const char* endpoint, const char* region, ICredentialProvider* credential_provider, const char* client_id)
{
    return MQTTMessageBroker::Create(ppObj, endpoint, region, credential_provider, client_id);
}