#ifndef __COMPTR_H__
#define __COMPTR_H__

#include <Panorama/comobj.h>

namespace Panorama
{
    template<typename T>
    class ComPtr
    {
    public:
        ComPtr(T* ptr)
        {
            Assign(ptr);
        }

        ComPtr(const ComPtr<T>& comPtr)
        {
            Assign(comPtr._ptr);
        }

        ComPtr()
        {
            _ptr = nullptr;
        }

        ~ComPtr()
        {
            Release();
        }

        void Attach(T* ptr)
        {
            _ptr = ptr;
        }

        T* Detach()
        {
            T* ptr = _ptr;
            _ptr = nullptr;
            return ptr;
        }

        T* operator->() const
        {
            return _ptr;
        }

        operator T* () const
        {
            return _ptr;
        }

        void operator=(const ComPtr<T>& other)
        {
            Assign(other._ptr);
        }

        void AddRef()
        {
            if (_ptr)
            {
                _ptr->AddRef();
            }
        }

        void Release()
        {
            if (_ptr && _ptr->Release() == 0)
            {
                _ptr = nullptr;
            }
        }

        T** AddressOf()
        {
            return &_ptr;
        }

        T* Ptr()
        {
            return _ptr;
        }
        
        template<typename Interface>
        Interface* QueryInterface()
        {
            if(_ptr == nullptr)
            {
                return nullptr;
            }

            Interface* ptr;
            if (SUCCEEDED(_ptr->QueryInterface(uuidof(Interface), reinterpret_cast<void**>(&ptr))))
            {
                return ptr;
            }

            return nullptr;
        }

        T* _ptr = nullptr;
    private:
        void Assign(T* ptr)
        {
            if(_ptr == ptr)
            {
                return;
            }

            if(_ptr)
            {
                _ptr->Release();
            }

            _ptr = ptr;
            if (_ptr)
            {
                _ptr->AddRef();
            }
        }
    };
}

#endif