#ifndef __APIDEFS_H__
#define __APIDEFS_H__

#include <stdint.h>
#include <type_traits>
#include <Panorama/guid.h>
#include <mutex>
#include <map>

typedef int32_t HRESULT;

#define S_OK HRESULT(0) 
#define S_FALSE HRESULT(1)
#define E_NOINTERFACE HRESULT(0x80004002)
#define E_POINTER HRESULT(0x80004003)
#define E_OUTOFMEMORY HRESULT(0x8007000E)
#define E_HANDLE HRESULT(0x80070006)
#define E_NOTIMPL HRESULT(0x80004001)
#define E_INVALIDARG HRESULT(0x80070057)
#define E_FAIL HRESULT(0x80004005)
#define E_TIMEOUT HRESULT(0x8001011F)
#define E_OUTOFRANGE HRESULT(0x80001009)
#define E_INVALID_STATE HRESULT(0x80290100)
#define E_NOT_FOUND HRESULT(0x80290101)

#define SUCCEEDED(X) X >= 0
#define FAILED(X) X < 0

#ifdef EXPORT
    #define DLLAPI extern "C" __attribute__((visibility("default")))
#else
    #define DLLAPI extern "C"
#endif

#define CREATE_COM(CLASS, COM_NAME)                             \
    CHECKNULL(ppObj, E_POINTER);                                \
    *ppObj = nullptr;                                           \
    ComPtr<CLASS> COM_NAME;                                     \
    COM_NAME.Attach(new (std::nothrow) CLASS());                \
    CHECKNULL(COM_NAME, E_OUTOFMEMORY);                         \
    TraceDebug("Created %s [%p]", #CLASS, COM_NAME.Ptr());      \
    com_obj_created(COM_NAME.Ptr(), #CLASS);        

#define COM_DTOR(CLASS) TraceDebug("Destroying object %s [%p]", #CLASS, this)
#define COM_DTOR_FIN(CLASS)                                 \
    TraceDebug("Objected destroyed %s [%p]", #CLASS, this); \
    com_obj_destroyed(this);

#define COM_FACTORY(CLASS, INITIALIZE)  \
    HRESULT hr = S_OK;                  \
    CREATE_COM(CLASS, ptr);             \
    CHECKHR(ptr->INITIALIZE);           \
    *ppObj = ptr.Detach();              \
    return hr

inline const char* ErrorCodeToString(HRESULT hr)
{
    switch(hr)
    {
        case S_OK:
            return "OK";
        case S_FALSE:
            return "No-Op [Success]";
        case E_NOINTERFACE:
            return "No Interface";
        case E_POINTER:
            return "Invalid out parameter pointer";
        case E_OUTOFMEMORY:
            return "Out of memory";
        case E_HANDLE:
            return "Handle invalid";
        case E_NOTIMPL:
            return "Not implemented";
        case E_INVALIDARG:
            return "Invalid argument";
        case E_FAIL:
            return "Generic failure";
        case E_TIMEOUT:
            return "Timeout";
        case E_NOT_FOUND:
            return "Not Found";
        case E_OUTOFRANGE:
            return "Out of range";
        default:
            return "Unknown error";
    }
}


DLLAPI const char* GetVersionString();
DLLAPI const char* GetMajorMinorVersionString();

template<typename Check, typename Target>
constexpr bool IsType() 
{
    return std::is_same<Check, Target>::value;
}

namespace Panorama
{
    template <typename T> const Guid& internal_uuidof();
}

#define COMMA ,
#define DEF_INTERFACE_UUID(Interface, UUID)                         \
        template <> inline const Guid& internal_uuidof<Interface>() \
        {                                                           \
            static const Guid uuid = GuidFromString(UUID);          \
            return uuid;                                            \
        }                                                           \

#define DEF_INTERFACE(Interface, UUID, Base)    \
    struct Interface;                           \
    DEF_INTERFACE_UUID(Interface, UUID)         \
    struct Interface : public virtual Base

DLLAPI void com_obj_created(void*, const char* identifier);
DLLAPI void com_obj_destroyed(void*);
DLLAPI HRESULT memcheck();
DLLAPI void enable_memcheck(bool);
#endif