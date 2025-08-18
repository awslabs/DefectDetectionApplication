#ifndef __PROPERTY_MANAGER_H__
#define __PROPERTY_MANAGER_H__

#include <map>
#include <string>
#include <mutex>

#include <nlohmann/json.hpp>

#include <Panorama/apidefs.h>
#include <Panorama/comptr.h>
#include <Panorama/properties.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/trace.h>

namespace Panorama
{
    class PropertyManager : public UnknownImpl<IUnknownAlias>
    {
    public:
        static HRESULT Create(PropertyManager** ppObj, const char* id)
        {
            HRESULT hr = S_OK;
            CHECKNULL(ppObj, E_POINTER);
            CHECKNULL(id, E_INVALIDARG);
            *ppObj = nullptr;

            ComPtr<PropertyManager> ptr;
            ptr.Attach(new (std::nothrow) PropertyManager());
            CHECKNULL(ptr, E_OUTOFMEMORY);
            ptr->_id = id;
            *ppObj = ptr.Detach();
            return hr;
        }

        HRESULT SetBatchProperty(IPropertyCollection** ppObj, nlohmann::json& json, bool remove_non_present = true)
        {
            HRESULT hr = S_OK;

            ComPtr<IPropertyCollection> collection;
            CHECKHR(CreatePropertyCollection(collection.AddressOf()));

            std::map<std::string, bool> inUpdate;
            CHECKHR(SetBatchPropertyInternal(collection, inUpdate, json, remove_non_present));

            // Caller has provided a location to store collection of properties changed
            if(ppObj != nullptr)
            {
                *ppObj = collection.Detach();
            }

            return hr;
        }

        template<typename T>
        HRESULT SetProperty(const char* key, T value)
        {
            HRESULT hr = S_OK;
            nlohmann::json jObj;

            try
            {
                jObj[key] = value;
            }
            catch(...)
            {
                // Technically jObj[key] can throw if value isn't an object, this would never happen with our usage.
                return E_FAIL;
            }

            return SetBatchProperty(nullptr, jObj, false);
        }

        HRESULT GetProperty(IProperty** ppObj, const char* key)
        {
            HRESULT hr = S_OK;
            CHECKNULL(ppObj, E_POINTER);
            CHECKNULL(key, E_INVALIDARG);
            std::string propertyName = key;

            *ppObj = nullptr;

            std::lock_guard<std::mutex> lk(_propertyMtx);

            // Get the property type
            std::map<std::string, PropertyType>::iterator iter;
            iter = _typeMapping.find(propertyName);
            if(iter == _typeMapping.end())
            {
                TraceVerbose("Could not find property %s in %s", key, _id.c_str());
                return E_NOT_FOUND;
            }

            TraceVerbose("Found property %s in %s", key, _id.c_str());
            PropertyType propType = iter->second;

            switch(propType)
            {
                case PropertyType::INT32:
                    _intProperties[propertyName].AddRef();
                    *ppObj = _intProperties[propertyName].Ptr();
                    break;
                case PropertyType::BOOL:
                    _boolProperties[propertyName].AddRef();
                    *ppObj = _boolProperties[propertyName].Ptr();
                    break;
                case PropertyType::FLOAT:
                    _floatProperties[propertyName].AddRef();
                    *ppObj = _floatProperties[propertyName].Ptr();
                    break;
                case PropertyType::STRING:
                    _stringProperties[propertyName].AddRef();
                    *ppObj = _stringProperties[propertyName].Ptr();
                    break;
                case PropertyType::UNKNOWN:
                    return E_NOTIMPL;
            }

            return hr;
        }

        std::string ToJson()
        {
            nlohmann::json json;
            for(const auto& elem : _stringProperties)
            {
                if(elem.second->IsJson())
                {
                    json[elem.first] = nlohmann::json::parse(elem.second->Get());
                }
                else
                {
                    json[elem.first] = elem.second->Get();
                }
            }

            for(const auto& elem : _intProperties)
            {
                json[elem.first] = elem.second->Get();
            }

            for(const auto& elem : _floatProperties)
            {
                json[elem.first] = elem.second->Get();
            }

            for(const auto& elem : _boolProperties)
            {
                json[elem.first] = elem.second->Get();
            }

            return json.dump();
        }

    protected:
        std::string _id;

        std::map<std::string, ComPtr<IStringProperty>> _stringProperties;
        std::map<std::string, ComPtr<IIntegerProperty>> _intProperties;
        std::map<std::string, ComPtr<IFloatProperty>> _floatProperties;
        std::map<std::string, ComPtr<IBooleanProperty>> _boolProperties;
        std::map<std::string, PropertyType> _typeMapping;
        std::map<std::string, ComPtr<IPropertyEventHandler>> _callbacks;

        std::mutex _propertyMtx;

    private:
        template<typename T>
        HRESULT EraseProperty(std::string& propId, std::map<std::string, ComPtr<T>>& properties)
        {
            if(properties.find(propId) == properties.end())
            {
                TraceWarning("Property %s not in specified container", propId.c_str());
                return E_NOT_FOUND;
            }
            else
            {
                if(_callbacks.find(propId) != _callbacks.end())
                {
                    properties[propId]->RemovePropertyEventHandler(_callbacks[propId]);
                    _callbacks.erase(propId);
                }
                
                properties.erase(propId);
                TraceInfo("Property %s was removed", propId.c_str());
            }

            return S_OK;
        }

        HRESULT RemoveProperty(std::string propId)
        {
            HRESULT hr = S_OK;

            TraceVerbose("Removing property %s", propId.c_str());
            if(_typeMapping.find(propId) == _typeMapping.end())
            {
                TraceWarning("Property %s is not in property manager", propId.c_str());
                return E_NOT_FOUND;
            }

            PropertyType propType = _typeMapping[propId];
            _typeMapping.erase(propId);
            switch(propType)
            {
                case PropertyType::INT32:
                    return EraseProperty(propId, _intProperties);
                case PropertyType::BOOL:
                    return EraseProperty(propId, _boolProperties);
                case PropertyType::FLOAT:
                    return EraseProperty(propId, _floatProperties);
                case PropertyType::STRING:
                    return EraseProperty(propId, _stringProperties);
                default:
                    return E_INVALID_STATE;
            }

            return hr;
        }

        template<typename T>
        HRESULT SetPropertyInternal(IPropertyCollection* collection, const char* key, PropertyType propType, T value, bool json=false)
        {
            HRESULT hr = S_OK;
            CHECKNULL(key, E_INVALIDARG);
            std::string propertyName = key;

            TraceVerbose("Setting property %s in %s as type %s", propertyName.c_str(), _id.c_str(), _typeToString[(int)propType].c_str());
            if(_typeMapping.find(propertyName) != _typeMapping.end())
            {
                // key already set, update value
                if(_typeMapping[propertyName] != propType)
                {
                    // Key is trying to be set for 2 different property types;
                    TraceError("Trying to set property %s as type %s but is already stored as type %s", propertyName.c_str(), _typeToString[(int)propType].c_str(), _typeToString[(int)_typeMapping[propertyName]].c_str());
                    return E_INVALIDARG;
                }

                if constexpr (std::is_same<T, int32_t>::value)
                {
                    hr = _intProperties[propertyName]->Set(value);
                    if(hr == S_OK && collection != nullptr)
                    {
                        CHECKHR(collection->Add(_intProperties[propertyName]));
                    }

                    return hr;
                }
                else if constexpr (std::is_same<T, float>::value)
                {
                    hr = _floatProperties[propertyName]->Set(value);
                    if(hr == S_OK && collection != nullptr)
                    {
                        CHECKHR(collection->Add(_floatProperties[propertyName]));
                    }

                    return hr;
                }
                else if constexpr (std::is_same<T, bool>::value)
                {
                    hr = _boolProperties[propertyName]->Set(value);
                    if(hr == S_OK && collection != nullptr)
                    {
                        CHECKHR(collection->Add(_boolProperties[propertyName]));
                    }

                    return hr;
                }
                else if constexpr (std::is_same<T, const char*>::value)
                {
                    hr = _stringProperties[propertyName]->Set(value);
                    if(hr == S_OK && collection != nullptr)
                    {
                        CHECKHR(collection->Add(_stringProperties[propertyName]));
                    }

                    return hr;
                }
                else
                {
                    return E_NOTIMPL;
                }
            }
            else
            {
                // new key
                ComPtr<IIntegerProperty> intProperty;
                ComPtr<IFloatProperty> floatProperty;
                ComPtr<IBooleanProperty> booleanProperty;
                ComPtr<IStringProperty> stringProperty;

                std::lock_guard<std::mutex> lk(_propertyMtx);
                _typeMapping[propertyName] = propType;

                if constexpr (std::is_same<T, int32_t>::value)
                {
                    CHECKHR(CreateIntegerProperty(intProperty.AddressOf(), propertyName.c_str(), value));
                    _intProperties[propertyName] = intProperty;
                    if(collection != nullptr)
                    {
                        CHECKHR(collection->Add(intProperty));
                    }
                }
                else if constexpr (std::is_same<T, float>::value)
                {
                    CHECKHR(CreateFloatProperty(floatProperty.AddressOf(), propertyName.c_str(), value));
                    _floatProperties[propertyName] = floatProperty;
                    if(collection != nullptr)
                    {
                        CHECKHR(collection->Add(floatProperty));
                    }
                }
                else if constexpr (std::is_same<T, bool>::value)
                {
                    CHECKHR(CreateBooleanProperty(booleanProperty.AddressOf(), propertyName.c_str(), value));
                    _boolProperties[propertyName] = booleanProperty;
                    if(collection != nullptr)
                    {
                        CHECKHR(collection->Add(booleanProperty));
                    }
                }
                else if constexpr (std::is_same<T, const char*>::value)
                {
                    if(json)
                    {
                        CHECKHR(CreateJsonProperty(stringProperty.AddressOf(), propertyName.c_str(), value));
                    }
                    else
                    {
                        CHECKHR(CreateStringProperty(stringProperty.AddressOf(), propertyName.c_str(), value));
                    }

                    _stringProperties[propertyName] = stringProperty;
                    if(collection != nullptr)
                    {
                        CHECKHR(collection->Add(stringProperty));
                    }
                }
                else
                {
                    return E_NOTIMPL;
                }
            }

            return hr;
        }

        HRESULT SetBatchPropertyInternal(IPropertyCollection* collection, std::map<std::string, bool>& inUpdate, nlohmann::json& json, bool remove_non_present)
        {
            HRESULT hr = S_OK;

            // if removing non present items go through map passed in
            // and initialize all to false as they have not yet been seen in the update
            if(remove_non_present)
            {
                for(const auto& elem : _typeMapping)
                {
                    inUpdate[elem.first] = false;
                }
            }

            for (nlohmann::json::iterator iter = json.begin(); iter != json.end(); iter++)
            {
                std::string propertyName = iter.key();
                inUpdate[propertyName] = true;
                if(iter.value().is_object() || iter.value().is_array())
                {
                    if(propertyName.compare("parameters") == 0)
                    {
                        CHECKHR(SetBatchPropertyInternal(collection, inUpdate, iter.value(), false));
                    }
                    else
                    {
                        std::string val = json[propertyName].dump();
                        CHECKHR(SetPropertyInternal(collection, propertyName.c_str(), Panorama::PropertyType::STRING, val.c_str(), true));
                    }
                }
                else if(iter.value().is_string())
                {
                    std::string val = json[propertyName];
                    CHECKHR(SetPropertyInternal(collection, propertyName.c_str(), Panorama::PropertyType::STRING, val.c_str()));
                }
                else if(iter.value().is_number_integer())
                {
                    int32_t val = json[propertyName];
                    CHECKHR(SetPropertyInternal(collection, propertyName.c_str(), Panorama::PropertyType::INT32, val));
                }
                else if(iter.value().is_number_float())
                {
                    double val = json[propertyName];
                    CHECKHR(SetPropertyInternal(collection, propertyName.c_str(), Panorama::PropertyType::FLOAT, static_cast<float>(val)));
                }
                else if(iter.value().is_boolean())
                {
                    bool val = json[propertyName];
                    CHECKHR(SetPropertyInternal(collection, propertyName.c_str(), Panorama::PropertyType::BOOL, val));
                }
                else
                {
                    TraceWarning("Value type for property %s is not supported", propertyName.c_str());
                    return E_NOTIMPL;
                }
            }

            // Remove any property that is not in this batch document
            if(remove_non_present)
            {
                for(const auto& elem : inUpdate)
                {
                    if(elem.second)
                    {
                        // property was in the update
                        continue;
                    }

                    RemoveProperty(elem.first);
                }
            }

            return hr;
        }

        inline static std::vector<std::string> _typeToString = {"INT32", "BOOL", "FLOAT", "STRING", "UNKNOWN"};
    };
}


#endif