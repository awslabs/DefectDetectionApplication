#ifndef __VECTOR_H__
#define __VECTOR_H__
#include <vector>

#include <Panorama/comobj.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/comptr.h>

namespace Panorama
{
    DEF_INTERFACE(IVector, "{BEB4E6FC-2E48-484B-B373-CC2E4C0E3A43}", IUnknownAlias)
    {
        virtual uint8_t* Data() const = 0;
        virtual size_t Count() const = 0;
        virtual HRESULT Resize(size_t count) = 0;
        
        template <typename T>
        inline T* DataAs() 
        {
            return reinterpret_cast<T*>(Data());
        }
    };

    DEF_INTERFACE(IInt64Vector, "{A60B58FB-C005-4E14-A53C-52FDF5455613}", IVector)
    {
        virtual int64_t Get(size_t idx) const = 0;
        virtual HRESULT Set(int64_t val, size_t idx) = 0;
    };

    DLLAPI HRESULT CreateInt64Vector(IInt64Vector** ppObj, size_t count);
    inline HRESULT CreateInt64Vector(IInt64Vector** ppObj, const std::vector<int64_t>& input)
    {
        HRESULT hr = S_OK;
        ComPtr<IInt64Vector> vect;
        CHECKHR(CreateInt64Vector(vect.AddressOf(), input.size()));
        CHECKIF(memcpy(vect->Data(), input.data(), input.size() * sizeof(int64_t)) != vect->Data(), E_FAIL);
        *ppObj = vect.Detach();
        return S_OK;
    }
}

#endif