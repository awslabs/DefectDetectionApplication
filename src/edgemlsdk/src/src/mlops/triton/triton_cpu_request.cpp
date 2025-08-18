#include <nlohmann/json.hpp>
#include <Panorama/apidefs.h>
#include <Panorama/flowcontrol.h>
#include "triton.h"

using namespace Panorama;

inline static std::map<std::string, TensorDataType> StringToDataType = 
{
    {"BOOL", TensorDataType::BOOL},
    {"UINT8", TensorDataType::UINT8},
    {"INT8", TensorDataType::INT8},
    {"BYTES", TensorDataType::BYTES},
    {"UINT16", TensorDataType::UINT16},
    {"INT16", TensorDataType::INT16},
    {"FP16", TensorDataType::FP16},
    {"BF16", TensorDataType::BF16},
    {"UINT32", TensorDataType::UINT32},
    {"INT32", TensorDataType::INT32},
    {"FP32", TensorDataType::FP32},
    {"UINT64", TensorDataType::UINT64},
    {"INT64", TensorDataType::INT64},
    {"FP64", TensorDataType::FP64}
};

inline static std::map<TensorDataType, TRITONSERVER_DataType> DataTypeToEnum = 
{
    {TensorDataType::BOOL, TRITONSERVER_TYPE_BOOL},
    {TensorDataType::UINT8, TRITONSERVER_TYPE_UINT8},
    {TensorDataType::INT8, TRITONSERVER_TYPE_INT8},
    {TensorDataType::BYTES, TRITONSERVER_TYPE_BYTES},
    {TensorDataType::UINT16, TRITONSERVER_TYPE_UINT16},
    {TensorDataType::INT16, TRITONSERVER_TYPE_INT16},
    {TensorDataType::FP16, TRITONSERVER_TYPE_FP16},
    {TensorDataType::BF16, TRITONSERVER_TYPE_BF16},
    {TensorDataType::UINT32, TRITONSERVER_TYPE_UINT32},
    {TensorDataType::INT32, TRITONSERVER_TYPE_INT32},
    {TensorDataType::FP32, TRITONSERVER_TYPE_FP32},
    {TensorDataType::UINT64, TRITONSERVER_TYPE_UINT64},
    {TensorDataType::INT64, TRITONSERVER_TYPE_INT64},
    {TensorDataType::FP64, TRITONSERVER_TYPE_FP64}
};

class TritonCpuRequest : public UnknownImpl<ITritonRequest>
{
public:
    static HRESULT Create(ITritonRequest** ppObj, ITritonServer* server, const std::string& modelName/*const std::string& input_name, const std::vector<int64_t>& input_shape, TRITONSERVER_DataType data_type, const std::string& output_name*/)
    {
        COM_FACTORY(TritonCpuRequest, Initialize(server, modelName));
    }

    ~TritonCpuRequest()
    {
        COM_DTOR(TritonCpuRequest);

        _inference_complete.Set();
        if(_request != nullptr)
        {
            TRITONSERVER_InferenceRequestDelete(_request);
            _request = nullptr;
        }

        if(_response_allocator != nullptr)
        {
            TRITONSERVER_ResponseAllocatorDelete(_response_allocator);
            _response_allocator = nullptr;
        }

        COM_DTOR_FIN(TritonCpuRequest);
    }

    HRESULT WaitForRequestToComplete(int32_t timeout) override
    {
        HRESULT hr = S_OK;
        if(timeout >= 0)
        {
            CHECKIF(_inference_complete.WaitFor(timeout) == false, E_TIMEOUT);
        }
        else
        {
            _inference_complete.Wait();
        }

        CHECKHR(_request_successful);
        return hr;
    }

    TRITONSERVER_InferenceRequest* Get() override
    {
        return _request;
    }

    HRESULT Input(ITensor** ppObj, int32_t idx) override
    {
        HRESULT hr = S_OK;

        CHECKNULL(ppObj, E_POINTER);
        CHECKIF(idx < 0 || idx >= _input_tensors.size(), E_OUTOFRANGE);

        *ppObj = nullptr;
        if(_input_tensors[idx] != nullptr)
        {
            _input_tensors[idx].AddRef();
            *ppObj = _input_tensors[idx];
        }

        return hr;
    }

    HRESULT SetInput(ITensor* tensor, int32_t idx) override
    {
        HRESULT hr = S_OK;
        CHECKNULL(tensor, E_INVALIDARG);
        CHECKIF(idx < 0 || idx >= _input_tensors.size(), E_OUTOFRANGE);

        if(_input_tensors[idx] != nullptr && _input_tensors[idx]->Abstract() == false)
        {
            // Isn't an abstract tensor, this means it was added as input, so we need to remove it
            CHECK_TRITON_RES(TRITONSERVER_InferenceRequestRemoveInput(_request, _input_tensors[idx]->Name()));
        }

        _input_tensors[idx] = tensor;

        if(_input_tensors[idx]->Abstract() == false)
        {
            // Isn't an abstract tensor, add this input
            ITensor* ptr = _input_tensors[idx];
            ComPtr<IInt64Vector> shape;
            CHECKHR(ptr->Shape(shape.AddressOf()));

            ComPtr<IBuffer> data;
            CHECKHR(ptr->Buffer(data.AddressOf()));

            CHECK_TRITON_RES(TRITONSERVER_InferenceRequestAddInput(_request, ptr->Name(), DataTypeToEnum[ptr->DataType()], shape->DataAs<int64_t>(), shape->Count()));
            CHECK_TRITON_RES(TRITONSERVER_InferenceRequestAppendInputData(_request, ptr->Name(), data->Data(), data->Size(), TRITONSERVER_MEMORY_CPU, 0));
        }

    Cleanup:
       return hr;
    }

    int32_t GetInputTensorIndex(const char* name) override
    {
        CHECKNULL_OR_EMPTY(name, -1);
        for(int32_t idx = 0; idx < _input_tensors.size(); idx++)
        {
            if(strcmp(_input_tensors[idx]->Name(), name) == 0)
            {
                return idx;
            }
        }

        return -1;
    }

    uint32_t GetNumOfInputTensors() override
    {
        return _input_tensors.size();
    }

    int32_t GetOutputTensorIndex(const char* name) override
    {
        CHECKNULL_OR_EMPTY(name, -1);
        for(int32_t idx = 0; idx < _output_tensors.size(); idx++)
        {
            if(strcmp(_output_tensors[idx]->Name(), name) == 0)
            {
                return idx;
            }
        }

        return -1;
    }

    uint32_t GetNumOfOutputTensors() override
    {
        return _output_tensors.size();
    }

    HRESULT Output(ITensor** ppObj, int32_t idx) override
    {
        CHECKNULL(ppObj, E_POINTER);
        CHECKIF(idx < 0, E_OUTOFRANGE);
        CHECKIF(idx >= _output_tensors.size(), E_OUTOFRANGE);

        _output_tensors[idx].AddRef();
        *ppObj = _output_tensors[idx];

        return S_OK;
    }

    HRESULT MoveOutput(ITensor** ppObj, int32_t idx) override
    {
        HRESULT hr = S_OK;

        CHECKNULL(ppObj, E_POINTER);
        CHECKIF(idx < 0, E_OUTOFRANGE);
        CHECKIF(idx >= _output_tensors.size(), E_OUTOFRANGE);

        ComPtr<ITensor> tensor;
        CHECKHR(CreateOutputTensor(tensor.AddressOf(), idx));

        *ppObj = _output_tensors[idx].Detach();
        _output_tensors[idx] = tensor;
        return S_OK;
    }

private:
    TRITONSERVER_InferenceRequest *_request = nullptr;
    TRITONSERVER_ResponseAllocator *_response_allocator = nullptr;

    std::vector<ComPtr<ITensor>> _input_tensors, _output_tensors;
    std::vector<ComPtr<IBuffer>> _result_buffers;

    nlohmann::json::array_t _ogOutputMetadata;
    std::string _id;
    std::string _model_name;
    AutoResetEvent _inference_complete;
    HRESULT _request_successful = S_OK;

    HRESULT Initialize(ITritonServer* server, const std::string& modelName)
    {
        HRESULT hr = S_OK;
        CHECKNULL(server, E_INVALIDARG);
        CHECKNULL_OR_EMPTY(modelName.c_str(), E_INVALIDARG);
        
        const char* metadata = server->ModelMetadata(modelName.c_str());
        CHECKNULL(metadata, E_FAIL);
        CHECKIF(nlohmann::json::accept(metadata) == false, E_FAIL);

        _model_name = modelName;
        _id = GuidToString(GenerateGuid());

        // Parse the model metadata
        nlohmann::json jObj = nlohmann::json::parse(metadata);
        nlohmann::json::array_t inputs = jObj["inputs"];

        // The output tensor shape can change with varying inputs with dynamically sized tensors
        // Hold onto the original shape so when MoveOutput is called the output tensor on this response
        // Holds the original metadata and not the metadta of the created tensor from a previous run
        _ogOutputMetadata = jObj["outputs"];

        _input_tensors.resize(inputs.size());
        _output_tensors.resize(_ogOutputMetadata.size());

        // Create the triton inference request
        // This request can be reused over several inferences.  Only needs to change if the input/output shape changes
        // Model is static, so this won't be the case
        CHECK_TRITON_RES(TRITONSERVER_InferenceRequestNew(&_request, server->Get(), modelName.c_str(), -1));
        CHECK_TRITON_RES(TRITONSERVER_InferenceRequestSetId(_request, _id.c_str()));

        // Create the input layers
        for(int32_t idx = 0; idx < inputs.size(); idx++)
        {
            ComPtr<IInt64Vector> shape;
            CHECKHR(CreateInt64Vector(shape.AddressOf(), inputs[idx]["shape"].get<std::vector<int64_t>>()));

            ComPtr<ITensor> tensor;
            std::string name = inputs[idx]["name"];
            CHECKHR(MLOps::Tensor(tensor.AddressOf(), name.c_str(), shape, StringToDataType[inputs[idx]["datatype"]], nullptr));
            CHECKHR(SetInput(tensor, idx));
        }

        // Create the output layers
        _result_buffers.resize(_ogOutputMetadata.size());
        for(int32_t idx = 0; idx < _ogOutputMetadata.size(); idx++)
        {
            ComPtr<ITensor> tensor;
            CHECKHR(CreateOutputTensor(tensor.AddressOf(), idx));
            _output_tensors[idx] = tensor;
        }

        // Define the memory allocation/callbacks
        CHECK_TRITON_RES(TRITONSERVER_ResponseAllocatorNew(&_response_allocator, TritonCpuRequest::ResponseAlloc, TritonCpuRequest::ResponseRelease, nullptr));
        CHECK_TRITON_RES(TRITONSERVER_InferenceRequestSetResponseCallback(_request, _response_allocator, this, TritonCpuRequest::InferResponseComplete, reinterpret_cast<void *>(this)));
        CHECK_TRITON_RES(TRITONSERVER_InferenceRequestSetReleaseCallback(_request, TritonCpuRequest::InferRequestComplete, reinterpret_cast<void *>(this)));

    Cleanup:
        return hr;
    }

    HRESULT CreateOutputTensor(ITensor** ppObj, int32_t idx)
    {
        HRESULT hr = S_OK;
        CHECKNULL(ppObj, E_POINTER);

        ComPtr<IInt64Vector> shape;
        CHECKHR(CreateInt64Vector(shape.AddressOf(), _ogOutputMetadata[idx]["shape"].get<std::vector<int64_t>>()));

        ComPtr<ITensor> tensor;
        std::string name = _ogOutputMetadata[idx]["name"];
        CHECKHR(MLOps::Tensor(tensor.AddressOf(), name.c_str(), shape, StringToDataType[_ogOutputMetadata[idx]["datatype"]], nullptr));
        CHECK_TRITON_RES(TRITONSERVER_InferenceRequestAddRequestedOutput(_request, name.c_str()));

        *ppObj = tensor.Detach();

    Cleanup:
        return hr;
    }

    static TRITONSERVER_Error *ResponseAlloc(TRITONSERVER_ResponseAllocator *allocator, const char *tensor_name, size_t byte_size, TRITONSERVER_MemoryType preferred_memory_type,
                                int64_t preferred_memory_type_id, void *userp, void **buffer, void **buffer_userp, TRITONSERVER_MemoryType *actual_memory_type, int64_t *actual_memory_type_id)
    {
        if(userp == nullptr)
        {
            return TRITONSERVER_ErrorNew(TRITONSERVER_ERROR_INVALID_ARG, "userp passed to response alloc was null.  Should be a TritonCpuRequest pointer");
        }

        return static_cast<TritonCpuRequest*>(userp)->_ResponseAlloc(allocator, tensor_name, byte_size, preferred_memory_type, preferred_memory_type_id, buffer, buffer_userp, actual_memory_type, actual_memory_type_id);
    }

    static TRITONSERVER_Error *ResponseRelease(TRITONSERVER_ResponseAllocator *allocator, void *buffer, void *buffer_userp, size_t byte_size, TRITONSERVER_MemoryType memory_type,int64_t memory_type_id)
    {
        return nullptr;
    }

    static void InferRequestComplete(TRITONSERVER_InferenceRequest *request, const uint32_t flags, void *userp)
    {
        if(userp != nullptr)
        {
            static_cast<TritonCpuRequest*>(userp)->_InferRequestComplete(request, flags);
        }
    }

    static void InferResponseComplete(TRITONSERVER_InferenceResponse *response, const uint32_t flags, void *userp)
    {
        if(userp != nullptr)
        {
            static_cast<TritonCpuRequest*>(userp)->_InferResponseComplete(response, flags, userp);
        }
    }

    TRITONSERVER_Error *_ResponseAlloc(TRITONSERVER_ResponseAllocator *allocator, const char *tensor_name, size_t byte_size, TRITONSERVER_MemoryType preferred_memory_type,
                                int64_t preferred_memory_type_id, void **out_buffer, void **buffer_userp, TRITONSERVER_MemoryType *actual_memory_type, int64_t *actual_memory_type_id)
    {
        HRESULT hr = S_OK;

        // Get the pointer to the output layer data
        int32_t idx = GetOutputTensorIndex(tensor_name);
        CHECKIF_MSG(idx < 0, TRITONSERVER_ErrorNew(TRITONSERVER_ERROR_INTERNAL, "Allocating response for an unlocateable output tensor"), "Output tensor name = %s", tensor_name);

        ComPtr<ITensor> tensor;
        CHECK_FAIL(Output(tensor.AddressOf(), idx), TRITONSERVER_ErrorNew(TRITONSERVER_ERROR_INTERNAL, "Could not retrieve output tensor"));

        ComPtr<IBuffer> buffer;
        CHECK_FAIL(tensor->Buffer(buffer.AddressOf()), TRITONSERVER_ErrorNew(TRITONSERVER_ERROR_INTERNAL, "Could not retrieve output tensor buffer"));

        // Initially attempt to make the actual memory type and id that we
        // allocate be the same as preferred memory type
        *actual_memory_type = preferred_memory_type;
        *actual_memory_type_id = preferred_memory_type_id;
        *buffer_userp = nullptr;

        // If 'byte_size' is zero just return 'buffer' == nullptr, we don't
        // need to do any other book-keeping.
        if (byte_size == 0) 
        {
            *out_buffer = nullptr;
            return nullptr;
        }

        switch (*actual_memory_type) 
        {
            case TRITONSERVER_MEMORY_CPU:
            default:
                *actual_memory_type = TRITONSERVER_MEMORY_CPU;
                if(buffer == nullptr || buffer->Size() != byte_size)
                {
                    // Allocate the buffer, but don't assign it to the output tensor as the shape might not have been defined yet.
                    // That is set in the ResponseComplete callback, so just hang onto a reference here.
                    ComPtr<IBuffer> new_buffer;
                    CHECK_FAIL(Buffer::Create(new_buffer.AddressOf(), byte_size), TRITONSERVER_ErrorNew(TRITONSERVER_ERROR_INTERNAL, "Failed to create buffer"));
                    buffer = new_buffer;
                }

                break;
        }

        _result_buffers[idx] = buffer;
        *out_buffer = buffer->Data();
        return nullptr;
    }

    void _InferRequestComplete(TRITONSERVER_InferenceRequest *request, const uint32_t flags)
    {
        _inference_complete.Set();
    }

    void _InferResponseComplete(TRITONSERVER_InferenceResponse *response, const uint32_t flags, void* userp)
    {
        HRESULT hr = S_OK;

        
        CHECK_TRITON_RES(TRITONSERVER_InferenceResponseError(response));

        // Update the output tensor with the response shape
        {
            const char* name;
            TRITONSERVER_DataType datatype;
            const int64_t *shape;
            uint64_t dim_count;
            uint32_t batch_size;
            const void *base;
            size_t byte_size;
            TRITONSERVER_MemoryType memory_type;
            int64_t memory_type_id;
            void *userp;

            uint32_t count;
            CHECK_TRITON_RES(TRITONSERVER_InferenceResponseOutputCount(response, &count));

            for(int32_t output_idx = 0; output_idx < count; output_idx++)
            {
                CHECK_TRITON_RES(TRITONSERVER_InferenceResponseOutput(response, output_idx, &name, &datatype, &shape, &dim_count, &base, &byte_size, &memory_type, &memory_type_id, &userp));
                // Get the output tensor associated with this response
                int32_t tensor_idx = this->GetOutputTensorIndex(name);
                CHECKIF_MSG(tensor_idx == -1, , "Could not find output tensor %s", name);

                ComPtr<ITensor> output_tensor;
                CHECK_FAIL(this->Output(output_tensor.AddressOf(), tensor_idx), );

                // Update the shape to be consistent with the output
                ComPtr<IInt64Vector> shape_vector;
                CHECK_FAIL(output_tensor->Shape(shape_vector.AddressOf()), );
                CHECK_FAIL(shape_vector->Resize(dim_count), );

                for(int64_t idx = 0; idx < shape_vector->Count(); idx++)
                {
                    shape_vector->Set(shape[idx], idx);
                }

                // Set the results on the output tensor
                // If buffer hasn't change this is a no-op
                CHECK_FAIL(output_tensor->SetBuffer(_result_buffers[tensor_idx]), );
            }
        }
        
    Cleanup:
        _request_successful = hr;
        TRITONSERVER_InferenceResponseDelete(response);
    }
};

DLLAPI HRESULT CreateTritonRequest(IInferenceRequest** ppObj, IInferenceServer* server, const char* modelName)
{
    HRESULT hr = S_OK;
    CHECKNULL(ppObj, E_POINTER);

    ComPtr<ITritonServer> triton_server = ComPtr<IInferenceServer>(server).QueryInterface<ITritonServer>();
    CHECKNULL(triton_server, E_NOINTERFACE);

    ComPtr<ITritonRequest> request;
    CHECKHR(TritonCpuRequest::Create(request.AddressOf(), triton_server, modelName));
    *ppObj = request.Detach();
    return hr;
}