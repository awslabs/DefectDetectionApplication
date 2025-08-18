#include <map>
#include <mutex>
#include <fstream>
#include <sstream>
#include <dlfcn.h>
#include <regex>

#include <nlohmann/json.hpp>

#include <Panorama/message_broker.h>
#include <Panorama/comobj.h>
#include <Panorama/comptr.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/aws.h>
#include <misc.h>
#include <env_vars.h>

#include "message_id_variables.h"


using namespace Panorama;

static std::recursive_mutex mtx;
static std::map<std::string, IMessageBroker*> message_broker_instances;
static std::string _default_config;

struct TargetContext
{
    ComPtr<IProtocolClient> Client;
    std::map<std::string, ComPtr<IProtocolClientEventHandler>> CallbackHandler;
    std::map<std::string, nlohmann::json> MessageOptions;
    std::map<std::string, ComPtr<IProtocolSubscription>> Subscriptions;
    std::vector<std::string> MessagesHandled;
    ComPtr<IProtocolFactory> Factory;
};

class MessageBrokerImpl : public UnknownImpl<IMessageBroker>
{
public:
    static HRESULT Create(IMessageBroker** ppObj, const char* config, ICredentialProvider* credentials)
    {
        HRESULT hr = S_OK;
        CREATE_COM(MessageBrokerImpl, ptr);
        CHECKHR(ptr->InitializeInternal(config, credentials));
        *ppObj = ptr.Detach();
        return hr;
    }

    ~MessageBrokerImpl()
    {
        COM_DTOR(MessageBrokerImpl);

        // Upon destruction of the object set the corresponding value in the map of message_broker_instances to nullptr
        // To allow for recreation of this object and not return a no longer valid pointer
        {
            std::lock_guard<std::recursive_mutex> lk(mtx);
            if(message_broker_instances.find(_config) != message_broker_instances.end())
            {
                message_broker_instances.erase(_config);
            }
        }

        // Given some of the factories could be loaded from dynamically loaded shared libraries
        // Clear out ot the target_factories so those objects can be released before their respective libraries
        // are unloaded.  Failure to do so will lead to a segmentation fault
        _target_factories.clear();

        // Close any libraries that were opened to load protocol client factories
        for(auto iter = _factory_handles.begin(); iter != _factory_handles.end(); iter++)
        {
            dlclose(iter->second);
        }

        COM_DTOR_FIN(MessageBrokerImpl);
    }

    HRESULT Publish(const char* message_id, IPayload* payload) override
    {
        return PublishInternal(message_id, payload, nullptr, false);
    }

    HRESULT PublishAsync(const char* message_id, IPayload* payload, IMessageBrokerEventHandler* handler) override
    {
        return PublishInternal(message_id, payload, handler, true);
    }

    int32_t Subscribe(const char* subscription_id, IMessageBrokerEventHandler* handler) override
    {
        std::lock_guard<std::mutex> lk(_subscribe_mtx);
        HRESULT hr = S_OK;

        // Always subscribe to loopback event broker
        ComPtr<ILoopbackSubscription> loopback_subscriber;
        CHECKHR(CreateLoopbackSubscription(loopback_subscriber.AddressOf(), subscription_id));
        ComPtr<IMessageBrokerEventHandler> sub_handler = handler;
        CHECKHR(_targets["loopback"].Client->Subscribe(loopback_subscriber, [sub_handler](IPayload* payload)
        {
            sub_handler->OnMessageReceived(payload);
        }));

        // Several subscriptions could happen in this method
        // This token, one returned by loop back, will be used to map to the other subscribe tokens
        // Use loopback instead of another client since loopback is used for all clients.
        // This gurantees that this token will be unique within context of Message Broker
        int32_t token = hr;
        _cancellation_tokens[token].push_back(std::pair<ComPtr<IProtocolClient>, int32_t>(_targets["loopback"].Client, token));

        // loop through all targets and see if they handle this subscription id
        for(auto iter = _targets.begin(); iter != _targets.end(); iter++)
        {
            TargetContext ctxt = iter->second;
            if(ctxt.Subscriptions.find(subscription_id) != ctxt.Subscriptions.end())
            {
                CHECKHR(ctxt.Client->Subscribe(ctxt.Subscriptions[subscription_id], [sub_handler](IPayload* payload)
                {
                    sub_handler->OnMessageReceived(payload);
                }));

                TraceVerbose("Subscribed to target %s:%s for subscription id '%s'", ctxt.Factory->ProtocolName(), iter->first.c_str(), subscription_id);
                _cancellation_tokens[token].push_back(std::pair<ComPtr<IProtocolClient>, int32_t>(ctxt.Client, hr));
            }
        }

        return token;
    }

    HRESULT Unsubscribe(int32_t cancellation_token) override
    {
        HRESULT hr = S_OK;

        std::lock_guard<std::mutex> lk(_subscribe_mtx);
        if(_cancellation_tokens.find(cancellation_token) == _cancellation_tokens.end())
        {
            // No subscription associated with this token
            TraceWarning("No subscription associated to token %d", cancellation_token);
            return S_FALSE;
        }

        // Unsubscribe from the protocol clients
        for(auto iter = _cancellation_tokens[cancellation_token].begin(); iter != _cancellation_tokens[cancellation_token].end(); iter++)
        {
            CHECKHR(iter->first->Unsubscribe(iter->second));
        } 

        _cancellation_tokens.erase(cancellation_token);
        return hr;
    }

    HRESULT AddProtocolFactory(IProtocolFactory* factory) override
    {
        CHECKNULL(factory, E_INVALIDARG);
        if(_target_factories.find(factory->ProtocolName()) != _target_factories.end())
        {
            TraceError("Factory for protocol %s already exists", factory->ProtocolName());
            return E_INVALIDARG;
        }

        _target_factories[factory->ProtocolName()] = factory;
        return S_OK;
    }

    HRESULT Initialize() override
    {
        HRESULT hr = S_OK;
        if(_intialized)
        {
            return S_FALSE;
        }

        nlohmann::json config_json = nlohmann::json::parse(_config);
        CHECKIF_MSG(ValidateJsonProperty<nlohmann::json::array_t>(config_json, "targets", false) == false, E_INVALIDARG, "Property 'targets' it not an array");
        CHECKIF_MSG(ValidateJsonProperty<nlohmann::json::array_t>(config_json, "pipes", false) == false, E_INVALIDARG, "Property 'pipes' it not an array");

        if(config_json.contains("targets"))
        {
            CHECKHR(CreateTargets(config_json["targets"], _credentials));
        }

        if(config_json.contains("pipes"))
        {
            CHECKHR(CreatePipes(config_json["pipes"]));
        }

        _intialized = true;
        return hr;
    }

private:
    MessageBrokerImpl() = default;

    HRESULT InitializeInternal(const char* config, ICredentialProvider* credentials)
    {
        HRESULT hr = S_OK;
        CHECKNULL_OR_EMPTY(config, E_INVALIDARG);
        CHECKIF_MSG(nlohmann::json::accept(config) == false, E_INVALIDARG, "Could not parse configuration as JSON");

        _config = config;
        _credentials = credentials;

        // Add target factories provided by SDK
        ComPtr<IProtocolFactory> file_factory;
        ComPtr<IProtocolFactory> gpio_factory;
        CHECKHR(MessageBroker::FileProtocolFactory(file_factory.AddressOf()));
        CHECKHR(MessageBroker::GPIOProtocolFactory(gpio_factory.AddressOf()));
        CHECKHR(this->AddProtocolFactory(file_factory));
        CHECKHR(this->AddProtocolFactory(gpio_factory));
        PEEKHR(AddInternalFactory("libpanorama.aws.so", "CreateMqttProtocolFactory"));
        PEEKHR(AddInternalFactory("libpanorama.aws.so", "CreateS3ProtocolFactory"));

        // Always add the loopback broker
        {
            TargetContext target;
            CHECKHR(CreateLoopbackProtocolClient(target.Client.AddressOf()));
            _targets["loopback"] = std::move(target);
        }

        return hr;
    }

    HRESULT CreateTargets(const nlohmann::json& targets, ICredentialProvider* credential_provider)
    {
        HRESULT hr = S_OK;

        for(auto iter = targets.begin(); iter != targets.end(); iter++)
        {
            CHECKIF_MSG(ValidateJsonProperty<const char *>((*iter), "protocol", true) == false, E_INVALIDARG, "Target 'protocol' is not defined or not a string");
            CHECKIF_MSG(ValidateJsonProperty<const char *>((*iter), "name", true) == false, E_INVALIDARG, "Target 'name' is not defined or not a string");

            std::string protocol = (*iter)["protocol"];
            std::string name = (*iter)["name"];

            // Get the JSON object that has the options used for creating the protocol client
            std::string ctor_options = protocol + "_options";
            CHECKIF_MSG(ValidateJsonProperty<nlohmann::json>((*iter), ctor_options) == false, E_INVALIDARG, "options for creating target need to be a JSON object");

            TraceVerbose("Adding protocol client %s:%s", protocol.c_str(), name.c_str());

            // Check name of the target is unique
            CHECKIF_MSG(_targets.find(name) != _targets.end(), E_INVALIDARG, "Multiple targets with the same name");

            // Check a factory for this protocol exists
            if(_target_factories.find(protocol) == _target_factories.end())
            {
                TraceError("No factory for protocol '%s' was found", protocol.c_str());
                return E_NOT_FOUND;
            };

            TargetContext ctxt;

            // Create the protocol client
            ctxt.Factory = _target_factories[protocol];
            CHECKHR(ctxt.Factory->CreateProtocol(ctxt.Client.AddressOf(), (*iter)[ctor_options].dump().c_str(), credential_provider));

            // Create subscriptions, if defined
            std::string sub_options = protocol + "_subscriptions";
            if(iter->contains(sub_options))
            {
                CHECKIF_MSG(ValidateJsonProperty<nlohmann::json::array_t>((*iter), sub_options) == false, E_INVALIDARG, "Subscription options is not a JSON array");
                nlohmann::json::array_t subscriptions = (*iter)[sub_options];
                for(auto sub_iter = subscriptions.begin(); sub_iter != subscriptions.end(); sub_iter++)
                {
                    CHECKIF_MSG(ValidateJsonProperty<const char*>((*sub_iter), "subscription_id") == false, E_INVALIDARG, "Target subscription did not specify 'subscription_id' or it's not a string");
                    std::string subscription_id = (*sub_iter)["subscription_id"];
                    CHECKIF_MSG(ctxt.Subscriptions.find(subscription_id) != ctxt.Subscriptions.end(), E_INVALIDARG, "Already created a subscription for this target and subscription_id");

                    ComPtr<IProtocolSubscription> subscription; 
                    CHECKHR(ctxt.Factory->CreateSubscription(subscription.AddressOf(), sub_iter->dump().c_str()));
                    ctxt.Subscriptions[subscription_id] = subscription;
                }
            }

            _targets[name] = std::move(ctxt);
        }

        return hr;
    }

    HRESULT CreatePipes(const nlohmann::json& pipes)
    {
        HRESULT hr = S_OK;

        // Loop through all pipes defined
        for(auto iter = pipes.begin(); iter != pipes.end(); iter++)
        {
            CHECKIF_MSG(ValidateJsonProperty<const char *>((*iter), "message_id", true) == false, E_INVALIDARG, "Pipe 'message_id' is not defined or is not a string");
            CHECKIF_MSG(ValidateJsonProperty<nlohmann::json::array_t>((*iter), "destinations", true) == false, E_INVALIDARG, "Pipe 'destinations' is not defined or is not a JSON array");

            std::string message_id = (*iter)["message_id"];
            nlohmann::json destinations = (*iter)["destinations"];

            // Loop through all destinations in a pipe
            for(auto dest_iter = destinations.begin(); dest_iter != destinations.end(); dest_iter++)
            {
                // Get the destination target
                CHECKIF_MSG(ValidateJsonProperty<const char *>((*dest_iter), "target_name", true) == false, E_INVALIDARG, "Destination 'target_name' is not defined or is not a string");
                std::string target_name = (*dest_iter)["target_name"];
                CHECKIF_MSG(_targets.find(target_name) == _targets.end(), E_INVALIDARG, "Destination specifies target that does not exist");

                // Get the message options
                std::string message_options_key = std::string(_targets[target_name].Factory->ProtocolName()) + "_message_options";
                if(ValidateJsonProperty<nlohmann::json>((*dest_iter), message_options_key, true) == false)
                {
                    TraceError("Destination for message-id %s did not contain %s, or it's not a JSON object", message_id.c_str(), message_options_key.c_str());
                    return E_INVALIDARG;
                };
                nlohmann::json message_options = std::move((*dest_iter)[std::move(message_options_key)]);

                // Validate the options
                CHECKHR(_targets[target_name].Factory->ValidateMessageOptions(message_options.dump().c_str()));

                // Set target information about this destination
                _targets[target_name].MessageOptions[message_id] = std::move(message_options);
                _targets[target_name].MessagesHandled.push_back(message_id);

                TraceVerbose("Message id %s will be routed to %s:%s", message_id.c_str(), _targets[target_name].Factory->ProtocolName(), target_name.c_str());
            }
        }

        return hr;
    }

    HRESULT PublishInternal(const char* message_id, IPayload* payload, IMessageBrokerEventHandler* handler, bool async)
    {
        HRESULT hr = S_OK;
        CHECKNULL_OR_EMPTY(message_id, E_INVALIDARG);

        // In the scenario where the handler is Python implementation (meaning developer is using the python bindings most likely)
        // It is likely that the handler->RefCount() will equal 0 (i.e. there are no NATIVE references), which is accurate.
        // In this method, specifically around PublishAsync, a reference will be captured by the lambda callback, then released in the lambda after publishing.
        // This can cause a race condition in that if there are multiple protocol clients to publish too it is possible for publish to finish
        // before the next publish_async is called, causing the ref count to go from 0 -> 1 -> 0.  When it transitions to 0 it will instruct Python
        // to decrement it's PYTHON reference count (which was incremeted by 1 when passed to C/C++), and depending on how the GC is feeling that day
        // it could clean up the object before the next PublishAsync call uses it, causing a seg fault.
        //
        // This wasn't caught in unit tests because the Python GC doesn't run until after we exit the unit test.  To reproduce needed to run
        // directly from python
        //
        // Solution is to add a native reference for the entirety of this method, this will ensure that all native references are captured correctly
        // TLDR; don't delete this line.
        ComPtr<IMessageBrokerEventHandler> event_handler = handler;

        // Loop through all targets
        for(auto iter = _targets.begin(); iter != _targets.end(); iter++)
        {
            // Check if this target handles this message-id, or if the target is the loopback client
            // Always publish to loopback, nothing will happen unless someone has subscribed to this message-id
            TargetContext ctxt = iter->second;
            bool is_loopback = ctxt.Client.Ptr() == _targets["loopback"].Client.Ptr();
            
            // determine if the message should be routed to the target
            MessageIdMatchResults matchResults;
            if(is_loopback == false)
            {
                CHECKHR(GetMessageIdVariables(&matchResults, message_id, ctxt.MessagesHandled));
            }

            if(is_loopback || matchResults.Matches)
            {
                ComPtr<IProtocolMessage> msg;
                if(is_loopback == false)
                {
                    // Check we options for this message
                    if(ctxt.MessageOptions.find(matchResults.MatchedMessageId) == ctxt.MessageOptions.end())
                    {
                        // Should never get here, but check just to be safe
                        TraceError("No message options specified for %s to %s:%s", message_id, ctxt.Factory->ProtocolName(), iter->first.c_str());
                        return E_INVALID_STATE;
                    }

                    // Create the message with the variables expanded
                    std::string message_options = ctxt.MessageOptions[matchResults.MatchedMessageId].dump();
                    ExpandMessageOption(message_options, matchResults);
                    hr = ctxt.Factory->CreateMessage(msg.AddressOf(), payload, message_options.c_str());
                    if(FAILED(hr))
                    {
                        TraceError("Failed to create message for %s to %s:%s [%s]", message_id, ctxt.Factory->ProtocolName(), iter->first.c_str(), ErrorCodeToString(hr));
                        return hr;
                    }
                }
                else
                {
                    ComPtr<ILoopbackMessage> loopback_msg;
                    CHECKHR(CreateLoopbackMessage(loopback_msg.AddressOf(), payload, message_id));
                    msg = static_cast<IProtocolMessage*>(loopback_msg.Ptr());
                }

                if(async == false)
                {
                    CHECKHR(ctxt.Client->Publish(msg));
                }
                else
                {
                    ComPtr<IPayload> payload_published = payload;
                    std::string message_id_published = message_id;
                    TraceInfo("Publishing message-id `%s`", message_id_published.c_str());
                    CHECKHR(ctxt.Client->PublishAsync(msg, [
                        publish_callback = ComPtr<IMessageBrokerEventHandler>(handler), // capture reference as this method can (probably will) before publish is complete
                        payload_published, 
                        id_published = std::move(message_id_published)](const char* friendly_name, IProtocolMessage* message, bool successful)
                    {
                        if(publish_callback != nullptr)
                        {
                            publish_callback->OnPublished(friendly_name, id_published.c_str(), payload_published, successful);
                        }
                    }));
                }
                
                if(FAILED(hr))
                {
                    TraceError("Failed to publish message-id `%s` to %s:%s [%s]", message_id, ctxt.Factory->ProtocolName(), iter->first.c_str(), ErrorCodeToString(hr));
                    return hr;
                }
            }
        }

        return hr;
    }

    HRESULT LoadFactory(IProtocolFactory** ppObj, const char* shared_library, const char* factory_method)
    {
        typedef HRESULT (*CreateFactory)(IProtocolFactory** ppObj);
        CHECKNULL(ppObj, E_POINTER);
        CHECKNULL_OR_EMPTY(shared_library, E_INVALIDARG);
        CHECKNULL_OR_EMPTY(factory_method, E_INVALIDARG);
        HRESULT hr = S_OK;

        void* handle = nullptr;
        if(_factory_handles.find(shared_library) == _factory_handles.end())
        {
            handle = dlopen(shared_library, RTLD_NOW);
            if(handle == nullptr)
            {
                TraceError("%s", dlerror());
                return E_FAIL;
            }

            _factory_handles[shared_library] = handle;
        }

        dlerror(); // clear any existing error
        handle = _factory_handles[shared_library];
        CreateFactory create_factory = (CreateFactory)dlsym(handle, factory_method);
        const char *dlsym_error = dlerror();
        if (dlsym_error) 
        {
            TraceError("%s", dlsym_error);
            dlclose(handle);
            return E_FAIL;
        }

        CHECKHR(create_factory(ppObj));
        return hr;
    };

    HRESULT AddInternalFactory(const char* shared_library, const char* factory_method)
    {
        HRESULT hr = S_OK;
        ComPtr<IProtocolFactory> factory;
        CHECKHR_MSG(LoadFactory(factory.AddressOf(), shared_library, factory_method), "Error invoking '%s' from '%s'", factory_method, shared_library);
        CHECKHR(this->AddProtocolFactory(factory));
        return hr;
    }

    bool _intialized = false;
    std::string _config;
    std::map<std::string, TargetContext> _targets;
    std::map<std::string, ComPtr<IProtocolFactory>> _target_factories;
    std::map<int32_t, std::vector<std::pair<ComPtr<IProtocolClient>, int32_t>>> _cancellation_tokens;
    std::mutex _subscribe_mtx;
    std::list<ComPtr<IMessageBrokerEventHandler>> _audit_callbacks;
    ComPtr<ICredentialProvider> _credentials;
    std::map<std::string, void*> _factory_handles;
};

DLLAPI void SetMessageBrokerDefaultConfig(const char* config)
{
    _default_config = config == nullptr ? "" : config;
}

DLLAPI HRESULT LoadMessageBrokerConfiguration(IBuffer** ppObj)
{
    HRESULT hr = S_OK;
    CHECKNULL(ppObj, E_POINTER);
    *ppObj = nullptr;

    if(_default_config.empty() == false)
    {
        // Load from default config first, if set
        TraceVerbose("Loading configuration from default value");
        return Buffer::CreateFromString(ppObj, _default_config.c_str());
    }

    // Load from file defined in environment variable
    TraceVerbose("Default value not set, checking MESSAGE_BROKER_CONFIG_FILE");
    std::string config_file = GetEnvVar("MESSAGE_BROKER_CONFIG_FILE");
    std::stringstream ss;
    ss << "";
    if(config_file.empty())
    {
        TraceVerbose("Environment variable MESSAGE_BROKER_CONFIG_FILE was not defined or is empty, using empty configuration");
        ss << "{}";
    }
    else
    {
        TraceVerbose("Loading configuration from MESSAGE_BROKER_CONFIG_FILE");
        std::ifstream fstream(config_file);
        CHECKIF_MSG(fstream.is_open() == false, E_NOT_FOUND, "Could not open file defined in MESSAGE_BROKER_CONFIG_FILE");
        ss << fstream.rdbuf();
    }

    CHECKHR(Buffer::CreateFromString(ppObj, ss.str().c_str()));
    return hr;
}

DLLAPI HRESULT CreateMessageBroker(IMessageBroker** ppObj, ICredentialProvider* credentials, const char* config, bool unique)
{
    HRESULT hr = S_OK;

    ComPtr<IBuffer> config_buf;
    if(config == nullptr || strlen(config) == 0)
    {
        CHECKHR(LoadMessageBrokerConfiguration(config_buf.AddressOf()));
    }
    else
    {
        CHECKHR(Buffer::CreateFromString(config_buf.AddressOf(), config));
    }

    if(unique)
    {
        TraceVerbose("Creating unique event broker");
        CHECKHR(MessageBrokerImpl::Create(ppObj, config_buf->AsString(), credentials));
    }
    else
    {
        std::lock_guard<std::recursive_mutex> lk(mtx);
        std::string schema_str = config_buf->AsString();
        if(message_broker_instances.find(schema_str) == message_broker_instances.end())
        {
            // new schema
            TraceVerbose("Creating new instance event broker");
            CHECKHR(MessageBrokerImpl::Create(ppObj, schema_str.c_str(), credentials));
            message_broker_instances[schema_str] = *ppObj;
        }
        else
        {
            // existing schema
            TraceVerbose("Event broker for schema exists, returning reference");
            message_broker_instances[schema_str]->AddRef();
            *ppObj = message_broker_instances[schema_str];
        }
    }

    return hr;
}