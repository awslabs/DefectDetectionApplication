#include <Panorama/message_broker.h>
#include <Panorama/flowcontrol.h>
#include <core/message_broker/protocol_client_base.h>
#include "periphery/gpio.h"

using namespace Panorama;

class GPIOMessage : public ProtocolMessageBase<IGPIOProtocolMessage>
{
public:
    static HRESULT Create(IGPIOProtocolMessage** ppObj, IPayload* payload, const char* rules , const char* signal_types, int64_t* pins, int64_t* pulse_width_ms, int64_t elem_count)
    {
        COM_FACTORY(GPIOMessage, Initialize(payload, rules, signal_types, pins,pulse_width_ms, elem_count));
    }

    ~GPIOMessage()
    {
        COM_DTOR_FIN(GPIOMessage);
    }

    const char* Rules() override
    {
        return _rules.c_str();
    }

    const char* SignalTypes() override
    {
        return _signal_types.c_str();
    }

    const int64_t* PulseWidthMs() override
    {
        return _pulse_width_ms.data();
    }

    const int64_t* Pins() override
    {
        return _pins.data();
    }

    const int64_t ElemCount() override
    {
        return _pins.size();
    }

private:
    GPIOMessage() = default;

    HRESULT Initialize(IPayload* payload, const char* rules, const char* signal_types, int64_t* pins, int64_t* pulse_width_ms, int64_t elem_count)
    {
        HRESULT hr = S_OK;
        CHECKHR(InitializeBase(payload));
        _rules = rules;
        _signal_types = signal_types;
        // Checked for element count parity done in factory call already.
        for(int i=0; i<elem_count; i++)
        {
            _pins.push_back(pins[i]);
            _pulse_width_ms.push_back(pulse_width_ms[i]);
        }
        return hr;
    }
    std::string _rules;
    std::string _signal_types;
    std::vector<int64_t> _pulse_width_ms;
    std::vector<int64_t> _pins;
}; 

DLLAPI HRESULT CreateGPIOProtocolMessage(IGPIOProtocolMessage** ppObj, IPayload* payload, const char* rule, const char* signal_type, int64_t* pin, int64_t* pulse_width_ms, int64_t elem_count)
{
    return GPIOMessage::Create(ppObj, payload, rule, signal_type, pin, pulse_width_ms, elem_count);
}
