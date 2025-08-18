#include <nlohmann/json.hpp>

#include <Panorama/message_broker.h>
#include <Panorama/flowcontrol.h>
#include <misc.h>
#include "periphery/gpio.h"

using namespace Panorama;

class GPIOProtocolFactory : public UnknownImpl<IProtocolFactory>
{
public:
    static HRESULT Create(IProtocolFactory** ppObj)
    {
        COM_FACTORY(GPIOProtocolFactory, Initialize());
    }

    ~GPIOProtocolFactory()
    {
        COM_DTOR_FIN(GPIOProtocolFactory);
    }

    HRESULT CreateProtocol(IProtocolClient** ppObj, const char* creation_options, ICredentialProvider* credential_provider) override
    {
        return MessageBroker::GPIOProtocolClient(ppObj);
    }

    HRESULT ValidateMessageOptions(const char* message_options) override
    {
        CHECKIF_MSG(nlohmann::json::accept(message_options) == false, E_INVALIDARG, "Could not parse message options for gpio protocol");
        nlohmann::json jObj = nlohmann::json::parse(message_options);

        CHECKIF_MSG(ValidateJsonProperty<const char*>(jObj, "rules", true) == false, E_INVALIDARG, "Parameter 'rules' is missing or not a string in gpio protocol message options");
        CHECKIF_MSG(ValidateJsonProperty<const char*>(jObj, "signal_types", true) == false, E_INVALIDARG, "Parameter 'signal_types' is mising or not a string in gpio protocol message options");
        CHECKIF_MSG(ValidateJsonProperty<const char*>(jObj, "pins", false) == false, E_INVALIDARG, "Parameter 'pins' is not a string in gpio protocol message options");
        CHECKIF_MSG(ValidateJsonProperty<const char*>(jObj, "pulse_width_ms", false) == false, E_INVALIDARG, "Parameter 'pulse_width_ms' is not a string in gpio protocol message options");

        return S_OK;
    }

    HRESULT CreateMessage(IProtocolMessage** ppObj, IPayload* payload, const char* message_options) override
    {
        HRESULT hr = S_OK;
        CHECKHR(ValidateMessageOptions(message_options));
        nlohmann::json jObj = nlohmann::json::parse(message_options);

        std::string rules = jObj["rules"];
        std::string signal_types = jObj["signal_types"];
        std::string pins = jObj["pins"];
        std::string pulse_width_ms = jObj["pulse_width_ms"];
        std::vector<std::string> pins_vec = SplitString(pins, ';');
        std::vector<int64_t> pins_int;
        for (const auto& pin : pins_vec)
        {
            pins_int.push_back(stoi(pin));
        }
        std::vector<std::string> pulse_width_ms_vec = SplitString(pulse_width_ms, ';');
        std::vector<int64_t> pulse_width_ms_int;
        for (const auto& pulse : pulse_width_ms_vec)
        {
            pulse_width_ms_int.push_back(stoi(pulse));
        }
        CHECKIF_MSG(pins_int.size() != pulse_width_ms_int.size(), E_INVALIDARG, "Parameter 'pins' and 'pulse_width_ms' must have the same number of elements");
        ComPtr<IGPIOProtocolMessage> msg;
        CHECKHR(MessageBroker::GPIOProtocolMessage(msg.AddressOf(), payload, rules.c_str(), signal_types.c_str(), pins_int.data(), pulse_width_ms_int.data(), pins_int.size()));

        *ppObj = msg.Detach();
        return hr;
    }

    HRESULT CreateSubscription(IProtocolSubscription** ppObj, const char* subscription_options) override
    {
        return E_NOTIMPL;
    }

    const char* ProtocolName() override
    {
        return "gpio";
    }

private:
    HRESULT Initialize()
    {
        return S_OK;
    }
};

DLLAPI HRESULT CreateGPIOProtocolFactory(IProtocolFactory** ppObj)
{
    return GPIOProtocolFactory::Create(ppObj);
}