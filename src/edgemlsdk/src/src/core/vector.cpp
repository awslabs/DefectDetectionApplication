#include <Panorama/vector.h>
#include <Panorama/comptr.h>
#include <Panorama/unknown.h>
#include <Panorama/flowcontrol.h>

using namespace Panorama;

template<typename Interface, typename ElemType>
class VectorBase : public UnknownImpl<Interface>
{
public:
    uint8_t* Data() const override
    {
        return const_cast<uint8_t*>(reinterpret_cast<const uint8_t*>(_data.data()));
    }

    size_t Count() const override
    {
        return _data.size();
    }

     HRESULT Resize(size_t count) override
    {
        CHECKIF(count <= 0, E_OUTOFRANGE);
        try
        {
            _data.resize(count);
            return S_OK;
        }
        catch(...)
        {
            return E_OUTOFMEMORY;
        }
    }

    ElemType Get(size_t idx) const override
    {
        return _data[idx];
    }

    HRESULT Set(ElemType val, size_t idx) override
    {
        CHECKIF(idx < 0 || idx >= _data.size(), E_OUTOFRANGE);
        _data[idx] = val;
        return S_OK;
    }

protected:
    HRESULT BaseInitialize(size_t count)
    {
        try
        {
            _data.resize(count);
        }
        catch(...)
        {
            return E_OUTOFMEMORY;
        }
        
        return S_OK;
    }

    std::vector<ElemType> _data;
};

#define VECTOR_IMPL(NAME, ELEM)                                     \
    class NAME : public VectorBase<I##NAME, ELEM>                   \
    {                                                               \
    public:                                                         \
        static HRESULT Create(I##NAME** ppObj, size_t count)        \
        {                                                           \
            COM_FACTORY(NAME, BaseInitialize(count));               \
        }                                                           \
                                                                    \
        ~NAME()                                                     \
        {                                                           \
            COM_DTOR_FIN(NAME)                                      \
        }                                                           \
    };                                                              \
                                                                    \
    DLLAPI HRESULT Create##NAME(I##NAME** ppObj, size_t count)      \
    {                                                               \
        return NAME::Create(ppObj, count);                          \
    }

VECTOR_IMPL(Int64Vector, int64_t);
