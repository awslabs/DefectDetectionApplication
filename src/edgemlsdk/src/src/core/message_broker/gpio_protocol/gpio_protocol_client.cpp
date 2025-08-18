#include <fstream>
#include <future>

#include <Panorama/message_broker.h>
#include <core/message_broker/protocol_client_base.h>
#include <scheduling.h>
#include <misc.h>
#include "periphery/gpio.h"

using namespace Panorama;

#define RULE_ANOMALY "Anomaly"
#define RULE_NORMAL "Normal"
#define RULE_ALL "All"

class GPIOProtocolClient : public ProtocolClientBase<IProtocolSubscription>
{
public:
    static HRESULT Create(IProtocolClient** ppObj)
    {
        COM_FACTORY(GPIOProtocolClient, Initialize());
    }

    ~GPIOProtocolClient()
    {
        COM_DTOR(GPIOProtocolClient);
        COM_DTOR_FIN(GPIOProtocolClient);
    }

    HRESULT Publish(IProtocolMessage* message) override
    {
        HRESULT hr = S_OK;
        ComPtr<IGPIOProtocolMessage> msg = ComPtr<IProtocolMessage>(message).QueryInterface<IGPIOProtocolMessage>();
        CHECKIF(msg == nullptr, E_NOINTERFACE);
        CHECKHR(this->EnforceRule(msg));
        return hr;
    }

    HRESULT PublishAsync(IProtocolMessage* message, IProtocolClientEventHandler* eventHandler) override
    {
        // For GPIO No Async calls for now, due to application requirements.
        return Publish(message);
    }

    const char* FriendlyName() override
    {
        return _friendly_name.c_str();
    }

    HRESULT Reconnect() override
    {
        return S_OK;
    }

protected:
    HRESULT OnSubscription(IProtocolSubscription* subscription) override
    {
        return E_NOTIMPL;
    }

    HRESULT OnUnsubscribe(IProtocolSubscription* subscription) override
    {
        return E_NOTIMPL;
    }

private:
    GPIOProtocolClient() = default;

    HRESULT Initialize()
    {
        return S_OK;
    }

    gpio_edge_t _StrToEdgeType(const char* signal_type) {
        if (strcmp(signal_type, "GPIO.RISING") == 0) {
            return GPIO_EDGE_RISING;
        } else if (strcmp(signal_type, "GPIO.FALLING") == 0) {
            return GPIO_EDGE_FALLING;
        } else if (strcmp(signal_type, "GPIO.BOTH") == 0) {
            return GPIO_EDGE_BOTH;
        } else {
            return GPIO_EDGE_NONE;
        }
    }
    HRESULT EnforceRule(ComPtr<IGPIOProtocolMessage> message)
    {
        HRESULT hr = S_OK;

        // Check the directory exists
        const char* rules = message->Rules();
        const char* signal_types = message->SignalTypes();
        std::vector<std::string> all_rules = SplitString(std::string(rules), ';');
        std::vector<std::string> all_signal_types = SplitString(std::string(signal_types), ';');
        CHECKIF_MSG(all_rules.size() != all_signal_types.size(),E_INVALIDARG, "Number of rules and signal types should be equal");
        const int64_t* pins = message->Pins();
        const int64_t* pulse_width_ms = message->PulseWidthMs();
        std::vector<std::future<HRESULT>> futures;

        // Get the buffer for the data
        ComPtr<IPayload> payload;
        CHECKHR(message->Payload(payload.AddressOf()));

        ComPtr<IBuffer> buffer;
        CHECKHR(payload->Serialize(buffer.AddressOf()));
        uint8_t* is_anomalous = reinterpret_cast<uint8_t*>(buffer->Data());
        std::string res = std::string(RULE_NORMAL);
        if (is_anomalous[0] == 1) {
            res = std::string(RULE_ANOMALY);
        }
        auto thread_fn = [=](const char* rule, const char* signal_type, int64_t pin, int64_t pulse_width_ms) -> HRESULT {
            gpio_edge_t edge_type = _StrToEdgeType(signal_type);
            gpio_t* gpio_out_set;
            gpio_out_set = gpio_new();
            bool value = false;
            if(rule == res || rule == std::string(RULE_ALL)){
                if(gpio_open_sysfs(gpio_out_set, pin, GPIO_DIR_OUT) < 0) {
                    TraceError("Error in gpio_open %s",gpio_errmsg(gpio_out_set));
                    gpio_free(gpio_out_set);
                    return E_FAIL;
                }
                if (edge_type == GPIO_EDGE_RISING) {
                    value = true;
                } else if (edge_type == GPIO_EDGE_FALLING) {
                    value = false;
                }
                if (gpio_write(gpio_out_set, value) < 0) {
                    TraceError("error in gpio_write(): %s\n", gpio_errmsg(gpio_out_set));
                    gpio_close(gpio_out_set);
                    gpio_free(gpio_out_set);
                    return E_FAIL;
                }
                TraceInfo("pin %d set to %s for %d ms", pin, signal_type, pulse_width_ms);
                std::this_thread::sleep_for(std::chrono::milliseconds(pulse_width_ms));
                if (gpio_write(gpio_out_set, !value) < 0) {
                    TraceError("error in gpio_write(): %s\n", gpio_errmsg(gpio_out_set));
                    gpio_close(gpio_out_set);
                    gpio_free(gpio_out_set);
                    return E_FAIL;
                }
                TraceInfo("pin %d reset to %s", pin, signal_type);
                gpio_close(gpio_out_set);
            }
            gpio_free(gpio_out_set);
            return S_OK;
        };
        bool value = false;
        for(int i=0; i<all_rules.size();i++){
            futures.emplace_back(std::async(std::launch::async, thread_fn, all_rules[i].c_str(), all_signal_types[i].c_str(), pins[i], pulse_width_ms[i]));
        }
        TraceInfo("Waiting for all %d threads to complete", all_rules.size());
        for (auto& f : futures) {
            TraceInfo("Waiting for thread");
            CHECKHR(f.get());
        }
        TraceInfo("All threads completed");
        return S_OK;
    }
    std::string _friendly_name = "gpio";
};

DLLAPI HRESULT CreateGPIOProtocolClient(IProtocolClient** ppObj)
{
    return GPIOProtocolClient::Create(ppObj);
}