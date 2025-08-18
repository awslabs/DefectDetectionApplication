#include <nlohmann/json.hpp>

#include <Panorama/comptr.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/properties.h>

#include "expansion_base.h"

using namespace Panorama;

class StringExpansion : public ExpansionBase<std::string>
{
public:
    static HRESULT Create(IVariableExpansion** ppObj, IStringProperty* property)
    {
        COM_FACTORY(StringExpansion, InitializeBase(property));
    }

    ~StringExpansion()
    {
        COM_DTOR_FIN(StringExpansion);
    }

    HRESULT Expand(IBuffer** ppObj) override
    {
        HRESULT hr = S_OK;
        CHECKNULL(ppObj, E_POINTER);
        CHECKHR(Parse());
        CHECKHR(CreateBufferFromString(ppObj, _value.c_str()));
        return hr;
    }

protected:
    HRESULT ParseValue(const std::string& value) override
    {
        _value = value;
        return S_OK;
    }

private:
    StringExpansion() = default;
    std::string _value;
};

DLLAPI HRESULT CreateStringExpansion(IVariableExpansion** ppObj, IStringProperty* property)
{
    return StringExpansion::Create(ppObj, property);
}