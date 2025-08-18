#ifndef __COMOBJ_H__
#define __COMOBJ_H__

#include <atomic>
#include <typeinfo>
#include <cxxabi.h>
#include <mutex>
#include <exception>

#include <assert.h>

#include <Panorama/unknown.h>
#include <Panorama/trace.h>
#include <Panorama/guid.h>

namespace Panorama
{
    #define uuidof(X) internal_uuidof<X>()
    template<typename T>
    std::string type_name()
    {
        int status;
        std::string tname = typeid(T).name();
        char *demangled_name = abi::__cxa_demangle(tname.c_str(), NULL, NULL, &status);
        if(status == 0) {
            tname = demangled_name;
            free(demangled_name);
        }   

        return tname;
    }

    template<typename Interface>
    class UnknownImpl : virtual public Interface
    {
    public:
        UnknownImpl() :
            _refCount(1)
        {
        }

        virtual ~UnknownImpl()
        {
        }

        uint32_t AddRef() override
        {
            int32_t newCount = ++_refCount;
            if(IsType<Interface, ITraceListener>() == false)
            {
                TraceDebug("Adding reference to object of type %s at %p.  Ref count = %d", type_name<Interface>().c_str(), this, newCount);
            }
            
            return newCount;
        }

        uint32_t Release() override
        {
            uint32_t newCount = --_refCount;
            if(IsType<Interface, ITraceListener>() == false)
            {
                TraceDebug("Removing reference to object of type %s at %p.  Ref count = %d", type_name<Interface>().c_str(), this, newCount);
            }

            if (newCount == 0)
            {
                delete this;
            }

            return newCount;
        }

        // Current implementation does not handle inheritance
        // need to circle back on this because it's pretty useless without that ability
        HRESULT QueryInterface(Guid uuid, void** ppObj) override
        {
            HRESULT hr = S_OK;
            if (ppObj == nullptr)
            {
                return E_POINTER;
            }

            hr = uuidof(Interface) == uuid ? S_OK : E_NOINTERFACE;
            if (SUCCEEDED(hr))
            {
                *ppObj = reinterpret_cast<void*>(this);
            }

            return hr;
        }

        uint32_t RefCount() override
        {
            return _refCount;
        }

        int64_t ObjectId() override
        {
            return 0;
        }

    protected:
        std::atomic<int32_t> _refCount;
    };
}

#endif