#ifndef __MLOPS_H__
#define __MLOPS_H__

#include <Panorama/apidefs.h>
#include <Panorama/unknown.h>
#include <Panorama/buffer.h>
#include <Panorama/vector.h>

namespace Panorama
{
    enum class TensorDataType
    {
        BOOL = 0,
        UINT8,
        INT8,
        BYTES,
        UINT16,
        INT16,
        FP16,
        BF16,
        UINT32,
        INT32,
        FP32,
        UINT64,
        INT64,
        FP64,
        END
    };

    DEF_INTERFACE(ITensor, "{6E7C211A-80F1-47DE-9462-FCCE0A0D1E95}", IUnknownAlias)
    {
        virtual const char* Name() = 0;
        virtual HRESULT Shape(IInt64Vector** ppObj) = 0;
        virtual TensorDataType DataType() = 0;
        virtual bool Abstract() = 0;
        virtual HRESULT Buffer(IBuffer** ppObj) = 0;
        virtual HRESULT SetBuffer(IBuffer* buffer) = 0;
    };

    DEF_INTERFACE(IInferenceRequest, "{EE92B592-AFB6-4EF0-BCB1-7CBCF1985C34}", IUnknownAlias)
    {
        virtual HRESULT WaitForRequestToComplete(int32_t timeout = -1) = 0;
        virtual int32_t GetInputTensorIndex(const char* name) = 0;
        virtual uint32_t GetNumOfInputTensors() = 0;
        virtual HRESULT Input(ITensor** ppObj, int32_t idx) = 0;
        virtual HRESULT SetInput(ITensor* tensor, int32_t idx) = 0;

        virtual int32_t GetOutputTensorIndex(const char* name) = 0;
        virtual uint32_t GetNumOfOutputTensors() = 0;
        virtual HRESULT Output(ITensor** ppObj, int32_t idx) = 0;
        virtual HRESULT MoveOutput(ITensor** ppObj, int32_t idx) = 0;
    };

    DEF_INTERFACE(IInferenceServer, "{FF9ADDA7-B127-48A1-9289-03CFF616DE15}", IUnknownAlias)
    {
        virtual HRESULT LoadModel(const char* modelName) = 0;
        virtual HRESULT UnloadModel(const char* modelName) = 0;
        virtual const char* ModelMetadata(const char* modelName) = 0;
        virtual const char* GetModelStatus(const char* modelName) = 0;

        virtual const char* ListModels() = 0;
        virtual HRESULT GetStatus(IBuffer** ppObj) = 0;
        virtual const char* GetMetrics() = 0;
        virtual HRESULT ProcessRequest(IInferenceRequest* request) = 0;
    };

    DLLAPI HRESULT CreateTritonInferenceServer(IInferenceServer** ppObj, const char* modelRepoPath, const char* tritonServerPath, bool unique);
    DLLAPI HRESULT CreateTritonRequest(IInferenceRequest** ppObj, IInferenceServer* server, const char* modelName);
    DLLAPI void ReleaseTritonInferenceServers();
    DLLAPI HRESULT CreateTensor(ITensor** ppObj, const char* name, IInt64Vector* shape, TensorDataType dataType, IBuffer* data);
    

    class MLOps
    {
    public:
        /// @brief Creates, or retrives, an instance of a Triton Inference Server
        /// @param ppObj Pointer to the created IInferenceServer object
        /// @param modelRepoPath The repository where the models to pull from are stored
        /// @param tritonServerPath The installation path of the triton server
        /// @param unique Flag indicating if the created inference server is a unique instance.  
        ///               If set to false (default) will check to see if an instance was created using the same modelRepoPath and tritonServerPath values, 
        ///               if so, will return a reference to that object instead of creating a new one. 
        /// @return S_OK on success.  Error code otherwise
        static HRESULT TritonInferenceServer(IInferenceServer** ppObj, const char* modelRepoPath, const char* tritonServerPath, bool unique=false)
        {
            return CreateTritonInferenceServer(ppObj, modelRepoPath, tritonServerPath, unique);
        }

        /// @brief Removes the internal reference to the non-uniquely instantatiated Triton inference servers 
        static void ReleaseTritonServers()
        {
            ReleaseTritonInferenceServers();
        }

        /// @brief Creates an inference request to be processed by a Triton IInferenceServer
        /// @param ppObj Pointer to the created IInferenceRequest
        /// @param server The server this request will be used with.  Undetermined behavior if used with another server.
        /// @param modelName The name of the model this request is for
        /// @return S_OK on success.  Error code otherwise
        static HRESULT TritonRequest(IInferenceRequest** ppObj, IInferenceServer* server, const char* modelName)
        {
            return CreateTritonRequest(ppObj, server, modelName);
        }

        /// @brief Creates a new tensor
        /// @param ppObj Pointer to the created ITensor
        /// @param name Name of the tensor
        /// @param shape Shape of the tensor
        /// @param dataType Data type of the tensor
        /// @param buffer The buffer containing the actual data.  If buffer is null and tensor is not abstract (i.e. all dimensions in shape > 0) then buffer will be allocated.
        /// @return S_OK on success.  Error code otherwise
        static HRESULT Tensor(ITensor** ppObj, const char* name, IInt64Vector* shape, TensorDataType dataType, IBuffer* buffer)
        {
            return CreateTensor(ppObj, name, shape, dataType, buffer);
        }

        /// @brief Creates a new tensor
        /// @param ppObj Pointer to the created ITensor
        /// @param name Name of the tensor
        /// @param shape Shape of the tensor as std::vector<int64_t>.  IInt64Vector will be created from this
        /// @param dataType Data type of the tensor
        /// @param buffer The buffer containing the actual data.  If buffer is null and tensor is not abstract (i.e. all dimensions in shape > 0) then buffer will be allocated.
        static HRESULT Tensor(ITensor** ppObj, const char* name, const std::vector<int64_t>& shape, TensorDataType dataType, IBuffer* data)
        {
            HRESULT hr = S_OK;
            ComPtr<IInt64Vector> shape_vect;
            CHECKHR(CreateInt64Vector(shape_vect.AddressOf(), shape));
            return CreateTensor(ppObj, name, shape_vect, dataType, data);
        }
    };
}

#endif