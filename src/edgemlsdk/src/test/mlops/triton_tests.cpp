#include <list>
#include <nlohmann/json.hpp>

#include <Panorama/comptr.h>
#include <mlops/triton/triton.h>
#include <TestUtils.h>
#include <nlohmann/json.hpp>
#include <chrono>
#include <thread>
using namespace Panorama;

TEST(MLOps, TritonTest)
{
    HRESULT hr = S_OK;
    std::string model_dir = BuildDirectory() + "/bin/model_repo";

    {
        ComPtr<IInferenceServer> server1, server2, server3;

        ASSERT_S(MLOps::TritonInferenceServer(server1.AddressOf(), model_dir.c_str(), TRITON_INSTALL_DIR));
        ASSERT_S(MLOps::TritonInferenceServer(server2.AddressOf(), model_dir.c_str(), TRITON_INSTALL_DIR, true));
        ASSERT_S(MLOps::TritonInferenceServer(server3.AddressOf(), model_dir.c_str(), TRITON_INSTALL_DIR));

        ASSERT_EQ(server1.Ptr(), server3.Ptr());
        ASSERT_TRUE(server1.Ptr() != server2.Ptr());

        MLOps::ReleaseTritonServers();
    }

    {
        // Failure Cases
        ComPtr<IInferenceServer> server;

        // Create the server and load the model
        ASSERT_F(MLOps::TritonInferenceServer(server.AddressOf(), nullptr, TRITON_INSTALL_DIR));
        ASSERT_F(MLOps::TritonInferenceServer(server.AddressOf(), model_dir.c_str(), nullptr));
        ASSERT_S(MLOps::TritonInferenceServer(server.AddressOf(), model_dir.c_str(), TRITON_INSTALL_DIR));
        ASSERT_S(server->LoadModel("doesn't exist"));
        ASSERT_S(server->UnloadModel("doesn't exist"));

        // Create a request against a model that hasn't been loaded
        ComPtr<IInferenceRequest> request;
        ASSERT_F(MLOps::TritonRequest(request.AddressOf(), server, "not loaded model"));
    }

    {
        // Model Test
        ComPtr<IInferenceServer> server;

        // Create the server and load the model
        ASSERT_S(MLOps::TritonInferenceServer(server.AddressOf(), model_dir.c_str(), TRITON_INSTALL_DIR));
        // Intial MetaData before load model
        const char* initial_meta_data = server->ModelMetadata("test_model");
        ASSERT_NE(initial_meta_data, nullptr);
        nlohmann::json meta_json = nlohmann::json::parse(std::string(initial_meta_data));
        ASSERT_TRUE(meta_json.contains("name"));
        ASSERT_TRUE(meta_json.contains("state"));
        ASSERT_FALSE(meta_json.contains("platform"));
        ASSERT_STREQ(meta_json["name"].get<std::string>().c_str(), "test_model");
        ASSERT_STREQ(meta_json["state"].get<std::string>().c_str(), "UNKNOWN");
        // Negative metadata test.
        const char* wrong_meta_data = server->ModelMetadata("not a model");
        ASSERT_EQ(wrong_meta_data, nullptr);
        // Test List Models
        const char* list_model_data = server->ListModels();
        ASSERT_NE(list_model_data, nullptr);
        nlohmann::json list_model_json = nlohmann::json::parse(std::string(list_model_data));
        // Only one model listed
        ASSERT_TRUE(list_model_json.contains("test_model"));
        nlohmann::json elem = list_model_json["test_model"];
        ASSERT_TRUE(elem.contains("name"));
        ASSERT_TRUE(elem.contains("state"));
        ASSERT_STREQ(elem["name"].get<std::string>().c_str(), "test_model");
        ASSERT_STREQ(elem["state"].get<std::string>().c_str(), "UNKNOWN");
        // Load the model, simply divides the input by 2
        ASSERT_S(server->LoadModel("test_model"));
        std::this_thread::sleep_for(std::chrono::milliseconds(1000));
        // Test Metadata after model load.
        const char* meta_data = server->ModelMetadata("test_model");
        meta_json = nlohmann::json::parse(std::string(meta_data));
        ASSERT_TRUE(meta_json.contains("name"));
        ASSERT_TRUE(meta_json.contains("state"));
        ASSERT_TRUE(meta_json.contains("platform"));
        ASSERT_TRUE(meta_json.contains("inputs"));
        ASSERT_TRUE(meta_json.contains("outputs"));
        ASSERT_STREQ(meta_json["name"].get<std::string>().c_str(), "test_model");
        ASSERT_STREQ(meta_json["state"].get<std::string>().c_str(), "READY");

        // Create the request
        ComPtr<IInferenceRequest> request;
        ASSERT_S(MLOps::TritonRequest(request.AddressOf(), server, "test_model"));

        // Get a pointer to the input index and retrieve in the input tensor
        int32_t input_idx = request->GetInputTensorIndex("input_0");
        ASSERT_EQ(input_idx, 0);
        ComPtr<ITensor> input_tensor;
        ASSERT_S(request->Input(input_tensor.AddressOf(), input_idx));

        // Validate tensor's properties are expected
        ASSERT_TRUE(strcmp(input_tensor->Name(), "input_0") == 0);
        ASSERT_FALSE(input_tensor->Abstract());
        ASSERT_EQ(input_tensor->DataType(), TensorDataType::FP32);

        {
            ComPtr<IInt64Vector> shape;
            ASSERT_S(input_tensor->Shape(shape.AddressOf()));
            ASSERT_EQ(shape->Count(), 2);
            ASSERT_EQ(shape->Get(0), 1);
            ASSERT_EQ(shape->Get(1), 1024);
        }

        ComPtr<IBuffer> input_data;
        ASSERT_S(input_tensor->Buffer(input_data.AddressOf()));
        ASSERT_EQ(1*1024*4, input_data->Size());

        // Populate the input buffer with some test data
        float* input = reinterpret_cast<float*>(input_data->Data());
        for(int32_t idx = 0; idx < 1024; idx++, input++)
        {
            *input = static_cast<float>(idx);
        }

        // Process the request
        ASSERT_S(server->ProcessRequest(request));

        // Get the results
        int32_t output_idx = request->GetOutputTensorIndex("output_0");
        ASSERT_EQ(output_idx, 0);

        ComPtr<ITensor> output_tensor;
        ASSERT_S(request->Output(output_tensor.AddressOf(), output_idx));

        // Validate output tensor
        ASSERT_TRUE(strcmp(output_tensor->Name(), "output_0") == 0);
        ASSERT_FALSE(output_tensor->Abstract());
        ASSERT_EQ(output_tensor->DataType(), TensorDataType::FP32);

        {
            ComPtr<IInt64Vector> shape;
            ASSERT_S(output_tensor->Shape(shape.AddressOf()));
            ASSERT_EQ(shape->Count(), 2);
            ASSERT_EQ(shape->Get(0), 1);
            ASSERT_EQ(shape->Get(1), 1024);
        }

        // Validate the results
        ComPtr<IBuffer> output_data;
        ASSERT_S(output_tensor->Buffer(output_data.AddressOf()));
        ASSERT_EQ(1*1024*4, output_data->Size());

        float* output = reinterpret_cast<float*>(output_data->Data());
        for(int32_t idx = 0; idx < 1024; idx++, output++)
        {
            ASSERT_EQ(*output, static_cast<float>(idx) / 2.0f);
        }

        // Update the input data and rerun request
        input = reinterpret_cast<float*>(input_data->Data());
        for(int32_t idx = 0; idx < 1024; idx++, input++)
        {
            *input = static_cast<float>(idx) * 2.0f;
        }

        ASSERT_S(server->ProcessRequest(request));

        // We didn't move the results out, so output_data should contain the new results
        output = reinterpret_cast<float*>(output_data->Data());
        for(int32_t idx = 0; idx < 1024; idx++, output++)
        {
            ASSERT_EQ(*output, static_cast<float>(idx));
        }

        // Move the results
        ComPtr<ITensor> moved;
        ASSERT_S(request->MoveOutput(moved.AddressOf(), output_idx));

        // process the request again, to validate new_output is populated
        ASSERT_S(server->ProcessRequest(request));

        ComPtr<ITensor> new_output;
        ASSERT_S(request->Output(new_output.AddressOf(), output_idx));
        ASSERT_TRUE(moved.Ptr() != new_output.Ptr());
        // Test metrics before unloading model.
        const char* before_unload_metrics = server->GetMetrics();
        ASSERT_TRUE(before_unload_metrics != nullptr);
        ASSERT_S(server->UnloadModel("test_model"));
        // Test Metadata after model unload.
        const char* unload_meta_data = server->ModelMetadata("test_model");
        meta_json = nlohmann::json::parse(std::string(unload_meta_data));
        ASSERT_TRUE(meta_json.contains("name"));
        ASSERT_TRUE(meta_json.contains("state"));
        ASSERT_FALSE(meta_json.contains("platform"));
        ASSERT_FALSE(meta_json.contains("input"));
        ASSERT_FALSE(meta_json.contains("output"));
        ASSERT_STREQ(meta_json["name"].get<std::string>().c_str(), "test_model");
        // Unload returns immediately.
        ASSERT_STREQ(meta_json["state"].get<std::string>().c_str(), "UNLOADING");
    }

    ReleaseTritonInferenceServers();
}

TEST(MLOps, DynamicTensors)
{
    HRESULT hr = S_OK;
    std::string model_dir = BuildDirectory() + "/bin/model_repo";

    ComPtr<IInferenceServer> server;
    ASSERT_S(MLOps::TritonInferenceServer(server.AddressOf(), model_dir.c_str(), TRITON_INSTALL_DIR));
    ASSERT_S(server->LoadModel("dynamic_model"));
    std::this_thread::sleep_for(std::chrono::milliseconds(1000));
    // Create the request
    ComPtr<IInferenceRequest> request;
    ASSERT_S(MLOps::TritonRequest(request.AddressOf(), server, "dynamic_model"));

    // Get the input tensor
    ComPtr<ITensor> input_tensor;
    ASSERT_S(request->Input(input_tensor.AddressOf(), 0));
    ASSERT_TRUE(input_tensor->Abstract());
    ASSERT_EQ(input_tensor->DataType(), TensorDataType::UINT8);
    {
        // Internal Buffer should be null
        ComPtr<IBuffer> input_data;
        ASSERT_S(input_tensor->Buffer(input_data.AddressOf()));
        ASSERT_TRUE(input_data == nullptr);
    }

    // Create a non abstract tensor and set the input tensor
    ComPtr<IBuffer> data;
    ASSERT_S(Buffer::CreateFromString(data.AddressOf(), "Hello world"));

    ComPtr<ITensor> new_tensor;
    ASSERT_S(MLOps::Tensor(new_tensor.AddressOf(), "input_0", { data->Size() }, TensorDataType::UINT8, data));
    ASSERT_S(request->SetInput(new_tensor, 0));

    // Process the request
    ASSERT_S(server->ProcessRequest(request));

    // Get the results and validate
    ComPtr<ITensor> output_tensor;
    ASSERT_S(request->Output(output_tensor.AddressOf(), 0));
    ASSERT_EQ(output_tensor->Abstract(), false);

    ComPtr<IBuffer> output_data;
    ASSERT_S(output_tensor->Buffer(output_data.AddressOf()));
    ASSERT_TRUE(output_data != nullptr);
    ASSERT_EQ(strcmp(output_data->AsString(), "Hello world"), 0);

    ReleaseTritonInferenceServers();
}