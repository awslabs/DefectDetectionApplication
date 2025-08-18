#ifndef __EXPANSION_BASE_H__
#define __EXPANSION_BASE_H__

#include <nlohmann/json.hpp>
#include <Panorama/properties.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/comptr.h>
#include <misc.h>

namespace Panorama
{
    template<typename ValueType>
    class ExpansionBase : public UnknownImpl<IVariableExpansion>
    {
    public:
        bool Immutable() const override
        {
            return _immutable;
        }

        const char* Id() const override
        {
            return _property->ID();
        }

        bool Stale() override
        {
            // Check if property is valid JSON.  If it isn't than return true
            // This will ultimately trigger a Expand -> Parse which will return failure code
            // Might be worth considering updating this API for Stale to return an HRESULT
            if(nlohmann::json::accept(_property->Get()) == false)
            {
                return true;
            } 

            if(ValidateJsonProperty<ValueType>(_jObj, "value", true) == false)
            {
                return true;
            }

            // Expansion is stale if 'value' has changed or if the derived class 
            // indicates through some other means that the expansion is stale
            nlohmann::json current_value = nlohmann::json::parse(_property->Get());
            return (current_value["value"] != _jObj["value"]) || IsStale();
        }

    protected:
        ExpansionBase() = default;

        HRESULT InitializeBase(IStringProperty* property)
        {
            CHECKNULL(property, E_INVALIDARG);
            _property = property;
            return S_OK;
        }

        virtual bool IsStale()
        {
            return false;
        }

        HRESULT Parse()
        {
            HRESULT hr = S_OK;

            CHECKIF_MSG(nlohmann::json::accept(_property->Get()) == false, E_INVALIDARG, "Could not parse json in value");
            _jObj = nlohmann::json::parse(_property->Get());

            CHECKIF_MSG(ValidateJsonProperty<ValueType>(_jObj, "value", true) == false, E_INVALIDARG, "Value property is not defined or not of correct type"); 
            CHECKIF_MSG(ValidateJsonProperty<bool>(_jObj, "immutable", false) == false, E_INVALIDARG, "immutable is not defined or not of type boolean");
            if(_jObj.contains("immutable"))
            {
                _immutable = _jObj["immutable"];
            }

            return ParseValue(_jObj["value"]);
        }

        virtual HRESULT ParseValue(const ValueType& current_value) = 0;

    private:
        ComPtr<IStringProperty> _property;
        nlohmann::json _jObj;
        ValueType _value;
        bool _immutable = false;
    };
}

#endif