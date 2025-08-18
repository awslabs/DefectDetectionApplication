#include <vector>

#include <Panorama/comptr.h>
#include <Panorama/buffer.h>
#include <Panorama/flowcontrol.h>

using namespace Panorama;

class BufferImpl : public UnknownImpl<IBuffer>
{
public:
    static HRESULT Create(IBuffer** ppObj, int32_t size)
    {
        HRESULT hr = S_OK;
        CREATE_COM(BufferImpl, ptr);
        CHECKIF(size < 0, E_OUTOFRANGE);

        // todo: exception check
        ptr->_data.resize(size);

        *ppObj = ptr.Detach();
        return hr;
    }

    static HRESULT Create(IBuffer** ppObj, const char* str)
    {
        HRESULT hr = S_OK;
        CREATE_COM(BufferImpl, ptr);
        CHECKNULL(str, E_INVALIDARG);

        // todo: exception check
        ptr->_data.resize(strlen(str) + 1);
        memcpy(&(ptr->_data[0]), str, strlen(str) + 1);

        *ppObj = ptr.Detach();
        return hr;
    }

    ~BufferImpl()
    {
        COM_DTOR_FIN(BufferImpl);
    }

    uint8_t* Data() const override
    {
        return const_cast<uint8_t*>(_data.data());
    }

    int32_t Size() const override
    {
        return static_cast<int32_t>(_data.size());
    }

    const char* AsString() const override
    {
        const char* buf = reinterpret_cast<const char*>(&(_data[0]));
        int32_t len = _data.size();

        if(_data[_data.size() - 1] == '\0')
        {
            _to_string = std::string(buf);
        }
        else
        {
            _to_string = std::string(buf, len);
        }

        return _to_string.c_str();
    }

private:
    BufferImpl() = default;
    mutable std::string _to_string;
    std::vector<uint8_t> _data;
};

DLLAPI HRESULT CreateBuffer(IBuffer** ppObj, int32_t size)
{
    return BufferImpl::Create(ppObj, size);
}

DLLAPI HRESULT CreateBufferFromString(IBuffer** ppObj, const char* str)
{
    return BufferImpl::Create(ppObj, str);
}

DLLAPI HRESULT CreateBufferFromFile(IBuffer** ppObj, const char* path)
{
    CHECKNULL(ppObj, E_POINTER);
    CHECKNULL_OR_EMPTY(path, E_INVALIDARG);

    *ppObj = nullptr;
    FILE* fptr = fopen(path, "rb");
    CHECKNULL(fptr, E_FAIL);
    fseek(fptr, 0, SEEK_END);
    long sz = ftell(fptr);
    fseek(fptr, 0, SEEK_SET);

    if(sz < 0)
    {
        fclose(fptr);
        return E_FAIL;
    }

    if(sz > INT32_MAX)
    {
        fclose(fptr);
        return E_NOTIMPL;
    }

    ComPtr<IBuffer> buffer;
    HRESULT hr = BufferImpl::Create(buffer.AddressOf(), static_cast<int32_t>(sz));
    if(FAILED(hr))
    {
        fclose(fptr);
        return hr;
    }

    size_t bytes_read = fread(buffer->Data(), sizeof(uint8_t), buffer->Size(), fptr);
    fclose(fptr);

    if(bytes_read != buffer->Size())
    {
        return E_NOTIMPL;
    }

    *ppObj = buffer.Detach();
    return S_OK;
}