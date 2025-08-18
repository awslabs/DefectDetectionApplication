#include <Panorama/mlops.h>
#include <Panorama/unknown.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/comptr.h>

using namespace Panorama;

inline static std::map<TensorDataType, int64_t> DataTypeToSize = 
{
    {TensorDataType::BOOL, sizeof(bool)},
    {TensorDataType::UINT8, sizeof(uint8_t)},
    {TensorDataType::INT8, sizeof(uint8_t)},
    {TensorDataType::BYTES, sizeof(uint8_t)},
    {TensorDataType::UINT16, sizeof(uint16_t)},
    {TensorDataType::INT16, sizeof(uint16_t)},
    {TensorDataType::FP16, sizeof(uint16_t)},
    {TensorDataType::BF16, sizeof(uint16_t)},
    {TensorDataType::UINT32, sizeof(uint32_t)},
    {TensorDataType::INT32, sizeof(uint32_t)},
    {TensorDataType::FP32, sizeof(uint32_t)},
    {TensorDataType::UINT64, sizeof(uint64_t)},
    {TensorDataType::INT64, sizeof(uint64_t)},
    {TensorDataType::FP64, sizeof(uint64_t)}
};

class Tensor : public UnknownImpl<ITensor>
{
public:
    static HRESULT Create(ITensor** ppObj, const char* name, IInt64Vector* shape, TensorDataType dataType, IBuffer* data)
    {
        COM_FACTORY(Tensor, Initialize(name, shape, dataType, data));
    }

    ~Tensor()
    {
        COM_DTOR_FIN(Tensor);
    }

    const char* Name() override
    {
        return _name.c_str();
    }

    HRESULT Shape(IInt64Vector** ppObj) override
    {
        CHECKNULL(ppObj, E_POINTER);
        _shape.AddRef();
        *ppObj = _shape;
        return S_OK;
    }

    TensorDataType DataType() override
    {
        return _dataType;
    }

    bool Abstract() override
    {
        return _abstract;
    }

    HRESULT Buffer(IBuffer** ppObj) override
    {
        CHECKNULL(ppObj, E_POINTER);

        *ppObj = nullptr;
        if(_data != nullptr)
        {
            _data.AddRef();
            *ppObj = _data;
        }

        return S_OK;
    }

    HRESULT SetBuffer(IBuffer* buffer) override
    {
        if(_data == buffer)
        {
            return S_FALSE;
        }

        // Possible the shape was modified
        ComputeSize();

        // Null is not valid for a non abstract tensor
        CHECKIF(_abstract == false && buffer == nullptr, E_INVALIDARG);

        // Setting a buffer of different size for a non abstract tensor is not allowed
        CHECKIF(_abstract == false && buffer != nullptr && buffer->Size() != _sz, E_INVALIDARG);

        _data = buffer;
        _abstract = buffer == nullptr;
        return S_OK;
    }

private:
    Tensor() = default;

    void ComputeSize()
    {
        _sz = 1;
        bool has_even_no_of_negative = false;
        uint8_t negative_count = 0;
        for(int64_t idx = 0; idx < _shape->Count(); idx++)
        {
            _sz *= _shape->Get(idx);
            if(_shape->Get(idx) < 0)
            {
               negative_count++;
            }
        }
        has_even_no_of_negative = (negative_count % 2 == 0) && (negative_count != 0);
        // Will always be negative. [-1,-1] for example yields 1, mathemathically. but indicating dynamic, we switch back to negative. see line 136.
        if (has_even_no_of_negative)
        {
            _sz *= -1;
        }
        _sz *= DataTypeToSize[_dataType];
    }

    HRESULT Initialize(const char* name, IInt64Vector* shape, TensorDataType dataType, IBuffer* data)
    {
        HRESULT hr = S_OK;

        CHECKNULL_OR_EMPTY(name, E_INVALIDARG);
        CHECKNULL(shape, E_INVALIDARG);
        CHECKIF(static_cast<int32_t>(dataType) < 0 || static_cast<int32_t>(dataType) >= static_cast<int32_t>(TensorDataType::END), E_OUTOFRANGE);

        _name = name;
        _shape = shape;
        _dataType = dataType;

        ComputeSize();

        CHECKIF(_sz == 0, E_INVALIDARG);
        CHECKIF(_sz > INT32_MAX, E_OUTOFRANGE); // Buffer doesn't support int64_t yet
        _abstract = _sz < 0;

        CHECKIF_MSG(_abstract && data != nullptr, E_INVALIDARG, "Creating an abstract tensor with concrete data is verboten");

        if (data == nullptr && _abstract == false)
        {
            // non abstract tensor, but no data provided, allocate the data
            CHECKHR(Buffer::Create(_data.AddressOf(), static_cast<int32_t>(_sz)));
        }
        else if (data != nullptr && _abstract == false)
        {
            // non abstract tensor, data provided, add a reference to it
            CHECKIF_MSG(data->Size() != _sz, E_INVALIDARG, "Input data size does not match the size of the buffer");
            _data = data;
        }
        else
        {
            // abstract tensor, so _data is null
            _data = nullptr;
        }

        return S_OK;
    }

    int64_t _sz = 1;
    std::string _name;
    ComPtr<IInt64Vector> _shape;
    TensorDataType _dataType;
    bool _abstract;
    ComPtr<IBuffer> _data;
};

DLLAPI HRESULT CreateTensor(ITensor** ppObj, const char* name, IInt64Vector* shape, TensorDataType dataType, IBuffer* data)
{
    return Tensor::Create(ppObj, name, shape, dataType, data);
}