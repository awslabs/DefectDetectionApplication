#ifndef __PROPERTIES_H__
#define __PROPERTIES_H__

#include <functional>

#include <Panorama/comobj.h>
#include <Panorama/comptr.h>
#include <Panorama/apidefs.h>
#include <Panorama/buffer.h>
#include <Panorama/flowcontrol.h>

namespace Panorama
{
    enum class PropertyType
    {
        INT32 = 0,
        BOOL,
        FLOAT,
        STRING,
        UNKNOWN
    };

    struct IProperty;

    DEF_INTERFACE(IPropertyEventHandler, "{8E641CCC-678D-4CB6-B648-3841B945B12B}", IUnknownAlias)
    {
        virtual void OnPropertyChanged(IProperty* property) = 0;
    };

    class PropertyEventHandlerImpl : public UnknownImpl<IPropertyEventHandler>
    {
    public:
        static HRESULT Create(IPropertyEventHandler** ppObj, std::function<void(IProperty*)> cb);
        ~PropertyEventHandlerImpl();
        void OnPropertyChanged(IProperty* property) override;

    private:
        std::function<void(IProperty*)> _cb;
    };

    DEF_INTERFACE(IProperty, "{F7012DF9-DED0-4DC4-AFF2-004A0CCBBD08}", IUnknownAlias)
    {
        virtual HRESULT Buffer(IBuffer** ppObj) const = 0;
        virtual PropertyType Type() const = 0;
        virtual const char* ID() const = 0;

        virtual HRESULT AddPropertyEventHandler(IPropertyEventHandler* onChangedHandler) = 0;
        virtual void RemovePropertyEventHandler(IPropertyEventHandler* onChangedHandler) = 0;

        inline ComPtr<IPropertyEventHandler> OnPropertyChanged(std::function<void(IProperty*)> cb)
        {
            HRESULT hr = S_OK;
            ComPtr<IPropertyEventHandler> handler;
            CHECK_FAIL(PropertyEventHandlerImpl::Create(handler.AddressOf(), std::move(cb)), nullptr);
            CHECK_FAIL(this->AddPropertyEventHandler(handler), nullptr);
            return handler;
        }
    };

    DEF_INTERFACE(IStringProperty, "{D499DF45-2F31-43C3-B078-3F58399AF884}", IProperty)
    {
        virtual const char* Get() const = 0;
        virtual HRESULT Set(const char* newValue) = 0;
        virtual bool IsJson() const = 0;
    };

    DEF_INTERFACE(IIntegerProperty, "{8F904259-6CDE-4C75-8B55-E2828E55345F}", IProperty)
    {
        virtual int32_t Get() const = 0;
        virtual HRESULT Set(int32_t newValue) = 0;
    };

    DEF_INTERFACE(IFloatProperty, "{D684F784-D93A-4073-A81C-8B89E7FF5551}", IProperty)
    {
        virtual float Get() const = 0;
        virtual HRESULT Set(float newValue) = 0;
    };

    DEF_INTERFACE(IBooleanProperty, "{FB7C03C1-72BB-4C62-A73B-F004CEE61DB6}", IProperty)
    {
        virtual bool Get() const = 0;
        virtual HRESULT Set(bool newValue) = 0;
    };

    DEF_INTERFACE(IPropertyCollection, "{050A201F-63A0-4A91-AB55-4F1BA7952359}", IUnknownAlias)
    {
        virtual int32_t Count() const = 0;
        virtual HRESULT Add(IProperty* pObj) = 0;
        virtual HRESULT Remove(IProperty* pObj) = 0;
        virtual HRESULT At(IProperty** ppObj, int32_t idx) = 0;
        virtual bool Contains(IProperty* pObj) = 0;
        virtual bool ContainsKey(const char* key) = 0;
    };

    DEF_INTERFACE(IPropertyDelegate, "{9103B88F-8478-4AFC-9D3C-577D31A1D019}", IUnknownAlias)
    {
        virtual HRESULT GetProperty(IProperty** ppObj, const char* property) = 0;
        virtual HRESULT Synchronize(IPropertyCollection** ppObj) = 0;
    };

    DEF_INTERFACE(IVariableExpansion, "{6D88ED8B-CB29-4EE1-95C6-BB200B97A944}", IUnknownAlias)
    {
        virtual HRESULT Expand(IBuffer** ppObj) = 0;
        virtual bool Immutable() const = 0;
        virtual const char* Id() const = 0;
        virtual bool Stale() = 0;
    };

    DLLAPI HRESULT CreateStringProperty(IStringProperty** ppObj, const char* id, const char* value);
    DLLAPI HRESULT CreateJsonProperty(IStringProperty** ppObj, const char* id, const char* value);
    DLLAPI HRESULT CreateIntegerProperty(IIntegerProperty** ppObj, const char* id, int value);
    DLLAPI HRESULT CreateFloatProperty(IFloatProperty** ppObj, const char* id, float value);
    DLLAPI HRESULT CreateBooleanProperty(IBooleanProperty** ppObj, const char* id, bool value);
    DLLAPI HRESULT CreatePropertyCollection(IPropertyCollection** ppObj);
    DLLAPI HRESULT CreateCLIPropertyDelegate(IPropertyDelegate** ppObj, int argc, char** argv);
    DLLAPI HRESULT CreateFilePropertyDelegate(IPropertyDelegate** ppObj, const char* file);
    DLLAPI HRESULT CreateStringExpansion(IVariableExpansion** ppObj, IStringProperty* property);

    class Property
    {
    public:
        static ComPtr<IStringProperty> Create(const char* id, const char* value)
        {
            HRESULT hr = S_OK;
            ComPtr<IStringProperty> ptr;
            CHECK_FAIL(CreateStringProperty(ptr.AddressOf(), id, value), nullptr);
            return ptr;
        }

        static ComPtr<IStringProperty> CreateJson(const char* id, const char* value)
        {
            HRESULT hr = S_OK;
            ComPtr<IStringProperty> ptr;
            CHECK_FAIL(CreateJsonProperty(ptr.AddressOf(), id, value), nullptr);
            return ptr;
        }

        static ComPtr<IIntegerProperty> Create(const char* id, int32_t value)
        {
            HRESULT hr = S_OK;
            ComPtr<IIntegerProperty> ptr;
            CHECK_FAIL(CreateIntegerProperty(ptr.AddressOf(), id, value), nullptr);
            return ptr;
        }

        static ComPtr<IFloatProperty> Create(const char* id, float value)
        {
            HRESULT hr = S_OK;
            ComPtr<IFloatProperty> ptr;
            CHECK_FAIL(CreateFloatProperty(ptr.AddressOf(), id, value), nullptr);
            return ptr;
        }

        static ComPtr<IBooleanProperty> Create(const char* id, bool value)
        {
            HRESULT hr = S_OK;
            ComPtr<IBooleanProperty> ptr;
            CHECK_FAIL(CreateBooleanProperty(ptr.AddressOf(), id, value), nullptr);
            return ptr;
        }

        static ComPtr<IPropertyCollection> CreateCollection()
        {
            HRESULT hr = S_OK;
            ComPtr<IPropertyCollection> ptr;
            CHECK_FAIL(CreatePropertyCollection(ptr.AddressOf()), nullptr);
            return ptr;
        }

        static ComPtr<IPropertyDelegate> CreateCLIPropertyDelegate(int argc, char** argv)
        {
            HRESULT hr = S_OK;
            ComPtr<IPropertyDelegate> ptr;
            CHECK_FAIL(Panorama::CreateCLIPropertyDelegate(ptr.AddressOf(), argc, argv), nullptr);
            return ptr;
        }

        /// @brief Creates a property delegate that pulls data from a file
        /// @param file The path to the file
        /// @return The created property delegate on success or nullptr on failure
        static ComPtr<IPropertyDelegate> CreateFilePropertyDelegate(const char* file)
        {
            HRESULT hr = S_OK;
            ComPtr<IPropertyDelegate> ptr;
            CHECK_FAIL(Panorama::CreateFilePropertyDelegate(ptr.AddressOf(), file), nullptr);
            return ptr;
        }
    };
}

#endif
