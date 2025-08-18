#ifndef __UNKNOWN_H__
#define __UNKNOWN_H__

#include <Panorama/apidefs.h>
#include <Panorama/guid.h>

namespace Panorama
{

    /// @brief Interface from which all public interfaces should derive.  Provides interfaces for reference counting and upcasting interfaces.
    struct IUnknownAlias
    {
    public:
        virtual ~IUnknownAlias()
        {
        }

        /// @brief Atomically increments the reference count by 1
        /// @return The new value for the reference count
        virtual uint32_t AddRef() = 0;

        /// @brief Atomically decrements the reference count by 1
        /// @return The new value for the reference count
        virtual uint32_t Release() = 0;

        /// @brief Determines if this object can be cast as another
        /// @param uuid The UUID of the interface to interpret this object as
        /// @param ppObj Address of the pointer to the object to hold the casted object
        /// @return 
        ///     S_OK: The object can be cast to the requested interface <br>
        ///     E_NOINTERFACE: The object can not be cast to the requested interface <br>
        ///     E_POINTER: ppObj is null
        virtual HRESULT QueryInterface(Guid uuid, void** ppObj) = 0;

        /// @brief Gets the current reference count
        /// @return The current reference count
        virtual uint32_t RefCount() = 0;

        /// @brief  Gets an integer that uniquely identifies the object.  
        ///         Necessary for upcasting in languages that don't support casting.
        ///         This is ONLY intended to be used in projected languages (e.g. Python)
        ///         But needs to be defined here.  Happy for other suggestions.
        /// @return Integer that uniquely identifies the object.
        virtual int64_t ObjectId() = 0;
    };
    typedef struct IUnknownAlias IUnknownAlias;
    
    template <> inline const Guid& internal_uuidof<IUnknownAlias>()
    {
        static const Guid uuid = GuidFromString("{00000000-0000-0000-0000-000000000001}");
        return uuid;
    }
}

#endif