#ifndef __COLLECTION_BASE_H__
#define __COLLECTION_BASE_H__

#include <list>
#include <mutex>

#include <Panorama/comptr.h>
#include <Panorama/flowcontrol.h>

namespace Panorama
{
    template<typename Interface, typename T>
    class CollectionBase : public UnknownImpl<Interface>
    {
    public:
        int32_t Count() const override
        {
            return _elements.size();
        }

        HRESULT Add(T* pObj) override
        {
            CHECKNULL(pObj, E_INVALIDARG);
            ComPtr<T> ptr = pObj;

            std::lock_guard<std::mutex> lk(_mtx);
            _elements.push_back(ptr);
            return S_OK;
        }

        HRESULT Remove(T* pObj) override
        {
            CHECKNULL(pObj, S_FALSE);

            std::lock_guard<std::mutex> lk(_mtx);
            _elements.remove(pObj);
            return S_OK;
        }

        HRESULT At(T** ppObj, int32_t idx) override
        {
            CHECKNULL(ppObj, E_POINTER);
            CHECKIF(idx < 0 || idx >= _elements.size(), E_OUTOFRANGE);

            std::lock_guard<std::mutex> lk(_mtx);

            auto it = _elements.begin();
            std::advance(it, idx);
            (*it).AddRef();
            *ppObj = *it;
            return S_OK;
        }

        bool Contains(T* pObj) override
        {
            HRESULT hr = S_OK;
            CHECKNULL(pObj, false);

            std::lock_guard<std::mutex> lk(_mtx);
            auto it = std::find(_elements.begin(), _elements.end(), pObj);
            return it != _elements.end();
        }

    protected:
        std::mutex _mtx;
        std::list<ComPtr<T>> _elements;
    };
}

#endif