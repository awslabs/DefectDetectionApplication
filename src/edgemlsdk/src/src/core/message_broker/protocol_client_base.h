#ifndef __MESSAGE_BROKER_BASE_H__   
#define __MESSAGE_BROKER_BASE_H__

#include <map>
#include <list>
#include <mutex>

#include <Panorama/comptr.h>
#include <Panorama/comobj.h>
#include <Panorama/message_broker.h>
#include <random>
#include <unordered_map>

namespace Panorama
{
    template<typename T>
    struct SubContext
    {
        ComPtr<T> Subscription;
        ComPtr<IProtocolClientEventHandler> Handler;
    };

    template <typename MessageType>
    class ProtocolMessageBase : public UnknownImpl<MessageType>
    {
    public:
        HRESULT Payload(IPayload** ppObj) override
        {
            HRESULT hr = S_OK;
            CHECKNULL(ppObj, E_POINTER);
            _payload.AddRef();
            *ppObj = _payload;
            return hr;
        }

    protected:
        ProtocolMessageBase() = default;

        HRESULT InitializeBase(IPayload* payload)
        {
            CHECKNULL(payload, E_POINTER);
            _payload = payload;
            return S_OK;
        }

        ComPtr<IPayload> _payload;
    };

    template<typename SubscriptionType>
    class ProtocolClientBase : public UnknownImpl<IProtocolClient>
    {
    public:
        ProtocolClientBase()
            : _generator(_rd()), _distribution(0, INT32_MAX)
        {
        }

        int32_t Subscribe(IProtocolSubscription* subscription, IProtocolClientEventHandler* eventHandler) override
        {
            HRESULT hr = S_OK;
            CHECKNULL(eventHandler, E_INVALIDARG);
            CHECKNULL(subscription, E_INVALIDARG);

            ComPtr<SubscriptionType> upcast = ComPtr<IProtocolSubscription>(subscription).QueryInterface<SubscriptionType>();
            CHECKNULL(upcast, E_NOINTERFACE);

            std::lock_guard<std::mutex> lk(_mtx);
            CHECKHR(OnSubscription(upcast));

            // Create a random cancellation token
            int32_t token;
            do
            {
                token = _distribution(_generator);
            } while(_subscriptions.find(token) != _subscriptions.end());

            _subscriptions[token] = { upcast, eventHandler };
            return token;
        }

        HRESULT Unsubscribe(int32_t cancellation_token) override
        {
            HRESULT hr = S_OK;
            if(_subscriptions.find(cancellation_token) == _subscriptions.end())
            {
                TraceWarning("No subscription associated to token %d", cancellation_token);
                return S_FALSE;
            }

            std::lock_guard<std::mutex> lk(_mtx);
            CHECKHR(OnUnsubscribe(_subscriptions[cancellation_token].Subscription));
            _subscriptions.erase(cancellation_token);
            return hr;
        }

        
    protected:
        virtual HRESULT OnSubscription(SubscriptionType* subscription) = 0;
        virtual HRESULT OnUnsubscribe(SubscriptionType* subscription) = 0;

        HRESULT InvokeOnMessageReceived(IPayload* data, std::function<bool(SubscriptionType*)> predicate)
        {
            CHECKNULL(predicate, E_INVALIDARG);

            std::map<int32_t, SubContext<SubscriptionType>> copy;

            {
                std::lock_guard<std::mutex> lk(_mtx);
                copy = _subscriptions;
            }

            // Iterate over all subscriptions and ask the child class if the event handler
            // associated with the given subscription should be invoked
            try
            {
                for(auto iter = copy.begin(); iter != copy.end(); iter++)
                {
                    if(predicate(iter->second.Subscription))
                    {
                        iter->second.Handler->OnMessageReceived(data);
                    }
                }
            }
            catch(const std::exception& e)
            {
                TraceError("Exception thrown invoking OnMessageReceived callback: %s", e.what());
                return E_FAIL;
            }

            return S_OK;
        }

        std::map<int32_t, SubContext<SubscriptionType>> _subscriptions;

    private:
        std::random_device _rd;
        std::mt19937 _generator;
        std::uniform_int_distribution<int32_t> _distribution;

        std::mutex _mtx;
    };

    const std::string ID_MACRO = "${id}";
    const std::string CORRELATION_ID_MACRO = "${c_id}";
    const std::string TIMESTAMP_MACRO = "${timestamp}";
    const std::string COUNT_MACRO = "${count}";

    // Method that handles the expansion of Macros supported by EdgeML-SDK
    inline std::string ExpandMacros(const char* str, IPayload* payload)
    {
        static std::mutex mtx;
        static std::map<std::string, int32_t> counter;
        
        CHECKNULL_OR_EMPTY(str, "");
        CHECKNULL(payload, "");

        // Map of macro -> where it is in the string
        std::unordered_map<std::string, size_t> macros
        {
            {ID_MACRO, std::string::npos}, 
            {CORRELATION_ID_MACRO, std::string::npos}, 
            {TIMESTAMP_MACRO, std::string::npos},
            {COUNT_MACRO, std::string::npos}
        };

        std::string res = str;
        size_t pos = 0;

        // Keep iterating until there are no macros left to replace
        while (true)
        {
            // Find the next instances of the macros to be considered for replacement that remain in the string
            bool foundAnyMacros = false;
            for (auto &entry : macros)
            {
                entry.second = res.find(entry.first, pos);
                if (entry.second != std::string::npos)
                {
                    foundAnyMacros = true;
                }
            }

            // If there were no macros found, there is nothing left to replace. Return the string as is.
            if (!foundAnyMacros)
            {
                return res;
            }

            // There were some macros found. Iterate through all of the macros to identify which one comes first for replacement.
            for (const auto &entry : macros)
            {
                // This macro is only considered if it was found
                bool replaceThisMacro = entry.second != std::string::npos;
                if (!replaceThisMacro)
                {
                    continue;
                }

                // Check all of the other macros to see if there are any that appear sooner in the string
                for (const auto &otherEntry : macros)
                {
                    // If any other macro was found and is found sooner in the string, this one ain't it.
                    if (entry.first != otherEntry.first && otherEntry.second != std::string::npos && otherEntry.second < entry.second)
                    {
                        replaceThisMacro = false;
                        break;
                    }
                }

                // Replace the macro and update our current position in the string, so that we don't consider already replaced text in future iterations
                if (replaceThisMacro)
                {
                    if (entry.first == ID_MACRO)
                    {
                        res.replace(entry.second, ID_MACRO.length(), payload->Id());
                        pos = entry.second + strlen(payload->Id());
                    }
                    else if (entry.first == CORRELATION_ID_MACRO)
                    {
                        res.replace(entry.second, CORRELATION_ID_MACRO.length(), payload->CorrelationId());
                        pos = entry.second + strlen(payload->CorrelationId());
                    }
                    else if (entry.first == TIMESTAMP_MACRO)
                    {
                        std::string ts = std::to_string(payload->Timestamp());
                        res.replace(entry.second, TIMESTAMP_MACRO.length(), ts);
                        pos = entry.second + ts.length();
                    }
                    else if (entry.first == COUNT_MACRO)
                    {
                        std::lock_guard<std::mutex> lk(mtx);
                        if (counter.find(str) == counter.end())
                        {
                            counter[str] = 0;
                        }

                        std::string count_str = std::to_string(counter[str]++);
                        res.replace(entry.second, COUNT_MACRO.length(), count_str);
                        pos = entry.second + count_str.length();
                    }
                    else
                    {
                        // Something is funky, should not get here
                        TraceWarning("No replacements made for string: %s", res.c_str());
                        return res;
                    }

                    // There must be one, and only one macro that exists in the string and comes first.
                    // Since we just found and replaced it, no point in checking the other macros in this iteration.
                    break;
                }
            }
        }
    }
}

#endif