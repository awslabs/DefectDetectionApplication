#include <mutex>
#include <atomic>
#include <list>

#include <nlohmann/json.hpp>

#include <Panorama/apidefs.h>
#include <Panorama/comptr.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/properties.h>

using namespace Panorama;

template <typename T>
class BaseProperty : public UnknownImpl<T>
{
public:
    HRESULT Buffer(IBuffer** ppObj) const override
    {
        std::lock_guard<std::mutex> lk(_mtx);
        CHECKNULL(ppObj, E_POINTER);
        _value.AddRef();
        *ppObj = _value;
        return S_OK;
    }

    ~BaseProperty()
    {
        TraceDebug("Deleting base property %p", static_cast<void*>(this));
    }

    PropertyType Type() const override
    {
        return _type;
    }

    const char* ID() const override
    {
        return _id.c_str();
    }

    HRESULT AddPropertyEventHandler(IPropertyEventHandler* eventHandler) override
    {
        HRESULT hr = S_OK;
        CHECKNULL(eventHandler, E_INVALIDARG);

        std::lock_guard<std::mutex> lk(_handlerMtx);
        _eventHandlers.push_front(eventHandler);
        return hr;
    }

    void RemovePropertyEventHandler(IPropertyEventHandler* eventHandler) override
    {
        std::lock_guard<std::mutex> lk(_handlerMtx);
        _eventHandlers.remove(eventHandler);
    }

protected:
    template<typename ValueType>
    HRESULT InitializeBase(const char* id, PropertyType type, ValueType value)
    {
        TraceDebug("Initializing base property %p", static_cast<void*>(this));
        HRESULT hr = S_OK;
        CHECKNULL(id, E_INVALIDARG);

        _type = type;
        _id = id;

        if constexpr (std::is_integral<ValueType>::value || std::is_floating_point<ValueType>::value)
        {    
             HRESULT hr = S_OK;
            CHECKHR(CreateBuffer(_value.AddressOf(), sizeof(ValueType)));
            CHECKNULL(memcpy(_value->Data(), &value, sizeof(ValueType)), E_FAIL);
            return hr;
        }
        else if constexpr (std::is_same<ValueType, const char*>::value)
        {
            return CreateBufferFromString(_value.AddressOf(), value);
        }
        else
        {
            return E_NOTIMPL;
        }

        return hr;
    }

    template<typename ValueType>
    HRESULT SetInternal(ValueType newData, int32_t len, bool raiseEvent = true)
    {
        HRESULT hr = S_OK;

        {
            std::lock_guard<std::mutex> lk(_mtx);

            if constexpr (std::is_same<ValueType, const char*>::value)
            {
                if(strcmp(reinterpret_cast<char *>(_value->Data()), newData) == 0)
                {
                    TraceVerbose("Property %s already set to new value, skipping property changed callback", ID());
                    return S_FALSE;
                }
            }
            else if(memcmp(_value->Data(), newData, len) == 0)
            {
                TraceVerbose("Property %s already set to new value, skipping property changed callback", ID());
                return S_FALSE;
            }

            if constexpr (std::is_same<ValueType, const char*>::value)
            {
                // if the string is of different size need to create a new buffer
                if(len != _value->Size() - 1)
                {
                    _value.Release();
                    CHECKHR(CreateBufferFromString(_value.AddressOf(), newData));
                }
                else
                {
                    CHECKNULL(memcpy(_value->Data(), newData, len), E_FAIL);
                }
            }
            else
            {
                CHECKNULL(memcpy(_value->Data(), newData, len), E_FAIL);
            }
        }

        if(raiseEvent)
        {
            TraceVerbose("Property '%s' changed, invoking callbacks", _id.c_str());
            RaisePropertyChangedEvent();
        }
        else
        {
            TraceVerbose("Property '%s' changed, skipping callbacks", _id.c_str());
        }

        return hr;
    }

    void RaisePropertyChangedEvent()
    {
        std::list<ComPtr<IPropertyEventHandler>> shallow;

        {
            std::lock_guard<std::mutex> lk(_handlerMtx);
            shallow = _eventHandlers;
        }

        for(auto iter = shallow.begin(); iter != shallow.end(); iter++)
        {
            (*iter)->OnPropertyChanged(this);
        }
    }

    std::list<ComPtr<IPropertyEventHandler>> _eventHandlers;
    std::mutex _handlerMtx;

    std::string _id;
    PropertyType _type;

    mutable std::mutex _mtx;
    mutable ComPtr<IBuffer> _value;
};

class StringProperty : public BaseProperty<IStringProperty>
{
public:
    static HRESULT Create(IStringProperty **ppObj, const char* id, const char* value, bool isJson)
    {
        HRESULT hr = S_OK;
        CREATE_COM(StringProperty, ptr);

        if(isJson)
        {
            CHECKIF_MSG(nlohmann::json::accept(value) == false, E_INVALIDARG, "Property was created as a json property, but trying to set a non json value");
            ptr->_jsonInternal = nlohmann::json::parse(value);
        }

        ptr->_isJson = isJson;
        CHECKHR(ptr->InitializeBase(id, PropertyType::STRING, value));
        *ppObj = ptr.Detach();
        return hr;
    }

    ~StringProperty()
    {
        COM_DTOR_FIN(StringProperty);
    }

    const char* Get() const override
    {
        std::lock_guard<std::mutex> lk(_mtx);
        return _value == nullptr ? nullptr : _value->AsString();
    }

    HRESULT Set(const char* newValue) override
    {
        if(_isJson)
        {
            CHECKIF_MSG(nlohmann::json::accept(newValue) == false, E_INVALIDARG, "Property was created as a json property, but trying to set a non json value");

            nlohmann::json jObj = nlohmann::json::parse(newValue);

            // Check if the JSON object is equivalent, not if the strings are equivalent
            if(jObj == _jsonInternal)
            {
                TraceVerbose("Property %s already set to new value, skipping property changed callback", ID());
                return S_FALSE;
            }

            _jsonInternal = std::move(jObj);
        }

        return SetInternal(newValue, strlen(newValue));
    }

    bool IsJson() const override
    {
        return _isJson;
    }

private:
    StringProperty() = default;
    bool _isJson = false;
    nlohmann::json _jsonInternal;
};

class IntegerProperty : public BaseProperty<IIntegerProperty>
{
public:
    static HRESULT Create(IIntegerProperty **ppObj, const char* id, int32_t value)
    {
        HRESULT hr = S_OK;
        CREATE_COM(IntegerProperty, ptr);
        CHECKHR(ptr->InitializeBase(id, PropertyType::INT32, value));
        *ppObj = ptr.Detach();
        return hr;
    }

    ~IntegerProperty()
    {
        COM_DTOR_FIN(IntegerProperty);
    }

    int32_t Get() const override
    {
        std::lock_guard<std::mutex> lk(_mtx);
        return _value == nullptr ? 0 : _value->AsInt();
    }

    HRESULT Set(int32_t newValue) override
    {
        return SetInternal(&newValue, sizeof(newValue));
    }

private:
    IntegerProperty() = default;
};

class FloatProperty : public BaseProperty<IFloatProperty>
{
public:
    static HRESULT Create(IFloatProperty **ppObj, const char* id, float value)
    {
        HRESULT hr = S_OK;
        CREATE_COM(FloatProperty, ptr);
        CHECKHR(ptr->InitializeBase(id, PropertyType::FLOAT, value));
        *ppObj = ptr.Detach();
        return hr;
    }

    ~FloatProperty()
    {
        COM_DTOR_FIN(FloatProperty);
    }

    float Get() const override
    {
        std::lock_guard<std::mutex> lk(_mtx);
        return _value == nullptr ? 0 : _value->AsFloat();
    }

    HRESULT Set(float newValue) override
    {
        return SetInternal(&newValue, sizeof(newValue));
    }

private:
    FloatProperty() = default;
};

class BoolProperty : public BaseProperty<IBooleanProperty>
{
public:
    static HRESULT Create(IBooleanProperty **ppObj, const char* id, bool value)
    {
        HRESULT hr = S_OK;
        CREATE_COM(BoolProperty, ptr);

        CHECKHR(ptr->InitializeBase(id, PropertyType::BOOL, value));
        *ppObj = ptr.Detach();
        return hr;
    }

    ~BoolProperty()
    {
        COM_DTOR_FIN(BoolProperty);
    }

    bool Get() const override
    {
        std::lock_guard<std::mutex> lk(_mtx);
        return _value == nullptr ? 0 : _value->AsBoolean();
    }

    HRESULT Set(bool newValue) override
    {
        return SetInternal(&newValue, sizeof(newValue));
    }

private:
    BoolProperty() = default;
};

DLLAPI HRESULT CreateStringProperty(IStringProperty** ppObj, const char* id, const char* value)
{
    return StringProperty::Create(ppObj, id, value, false);
}

DLLAPI HRESULT CreateJsonProperty(IStringProperty** ppObj, const char* id, const char* value)
{
    return StringProperty::Create(ppObj, id, value, true);
}

DLLAPI HRESULT CreateIntegerProperty(IIntegerProperty** ppObj, const char* id, int value)
{
    return IntegerProperty::Create(ppObj, id, value);
}

DLLAPI HRESULT CreateFloatProperty(IFloatProperty** ppObj, const char* id, float value)
{
    return FloatProperty::Create(ppObj, id, value);
}

DLLAPI HRESULT CreateBooleanProperty(IBooleanProperty** ppObj, const char* id, bool value)
{
    return BoolProperty::Create(ppObj, id, value);
}