#include <thread>
#include <functional>

#include <gtest/gtest.h>
#include <nlohmann/json.hpp>

#include <Panorama/comptr.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/gst.h>
#include <Panorama/eventing.h>
#include <Panorama/app.h>
#include <Panorama/properties.h>

#include <TestUtils.h>

using namespace Panorama;

class TestPipelineEventHandler : public UnknownImpl<IPipelineEventHandler>
{
public:
    static HRESULT Create(TestPipelineEventHandler** ppObj)
    {
        HRESULT hr = S_OK;
        CREATE_COM(TestPipelineEventHandler, ptr);
        *ppObj = ptr.Detach();
        return hr;
    }

    ~TestPipelineEventHandler()
    {
        COM_DTOR_FIN(TestPipelineEventHandler)
    }

    void SetOnError(std::function<void(IPipeline* sender, IPipelineError*)> cb)
    {
        _onErrorCb =  std::move(cb);
    }

    void OnError(IPipeline* sender, IPipelineError* error) override
    {
        if(_onErrorCb)
        {
            _onErrorCb(sender, error);
        }
    }
    void OnRemovedFromPipeline(IPipeline* sender) override{}

    void SetOnStateChanged(std::function<void(IPipeline* sender, int32_t, int32_t)> cb)
    {
        _onStateChangeCb =  std::move(cb);
    }

    void OnStateChanged(IPipeline* sender, int32_t oldState, int32_t newState) override
    {
        if(_onStateChangeCb)
        {
            _onStateChangeCb(sender, oldState, newState);
        }
    }

    std::function<void(IPipeline*, IPipelineError*)> _onErrorCb;
    std::function<void(IPipeline*, int32_t, int32_t)> _onStateChangeCb;
};

TEST(RetryTests, RetryMechanismInputTests)
{
    HRESULT hr = S_OK;
    int argc;
    char** argv;

    {
        TraceInfo("------ Test 1 Valid Custom Retry ------");
        ComPtr<IStringProperty> property = Property::Create("retry", "{\"Mode\":\"linear\", \"Min\":0, \"Max\":5000, \"Increment\":1000, \"Messages\":[{\"Type\":1, \"Domain\":0, \"Code\":0}]}");
        ComPtr<IPipelineEventHandler> retry;
        ASSERT_S(CreateRetryMechanismFromProperty(retry.AddressOf(), property));
    }

    {
        TraceInfo("------ Test 2 Default Retry ------");
        ComPtr<IPipelineEventHandler> retry;
        ASSERT_S(CreateRetryMechanismDefault(retry.AddressOf()));
    }  
}

TEST(GstRunnerTests, RetryMechanismFunctionTests)
{
    HRESULT hr = S_OK;
    int argc;
    char** argv;
    
    GStreamer::Initialize(2);

    {
        TraceInfo("------ Test 1: Retry succeeds after Error ------");
        ComPtr<IApp> app = App::Create();

        // Create Pipeline
        ComPtr<IPipeline> pipeline;
        ASSERT_S(CreatePipeline(pipeline.AddressOf(), "id", "videotestsrc ! fakesink", app));

        // Create PipelineErrorCollection
        ComPtr<IPipelineErrorCollection> collection;
        CreatePipelineErrorCollection(collection.AddressOf());
        ComPtr<IPipelineError> pipeline_error;
        CreatePipelineError(pipeline_error.AddressOf(), GST_MESSAGE_ERROR, int(ErrorDomain::CORE), GST_CORE_ERROR_FAILED);
        collection->Add(pipeline_error);

        // Create retry mechanism
        ComPtr<IPipelineEventHandler> retryMechanism;
        ASSERT_S(CreateRetryMechanism(retryMechanism.AddressOf(), RetryMode::Linear, 0, 500, 100, collection));
        ASSERT_S(pipeline->AddPipelineEventHandler(retryMechanism));
        
        // Create TestPipelineEventHandler
        ComPtr<TestPipelineEventHandler> pipelineEventHandler;
        ASSERT_S(TestPipelineEventHandler::Create(pipelineEventHandler.AddressOf()));
        bool afterError = false;
        pipelineEventHandler->SetOnError([&](IPipeline* sender, IPipelineError* error)
        {
            afterError = true;
        });
        ManualResetEvent playingEvt;
        pipelineEventHandler->SetOnStateChanged([&](IPipeline* sender, int32_t oldState, int32_t newState)
        {
            if(newState == static_cast<int32_t>(GST_STATE_PLAYING) && afterError)
            {
                playingEvt.Set(); 
            }
        });
        ASSERT_S(pipeline->AddPipelineEventHandler(pipelineEventHandler));
        ASSERT_S(pipeline->Start());

        // Create a custom error msg
        GstMessage *msg;
        GstBus *bus = gst_pipeline_get_bus(GST_PIPELINE(pipeline->Element()));
        GError *error = g_error_new(GST_CORE_ERROR, GST_CORE_ERROR_FAILED, "Error Message");
        msg = gst_message_new_error(GST_OBJECT(pipeline->Element()), error, "Error for retry test");
        TraceInfo("Post an error to gst bus");
        gst_bus_post(bus, msg);
        g_clear_error(&error);
        gst_object_unref(bus); 

        ASSERT_TRUE(playingEvt.WaitFor(3000));
        pipeline->RemovePipelineEventHandler(pipelineEventHandler);
        pipeline->RemovePipelineEventHandler(retryMechanism);
    }
    {
        TraceInfo("------ Test 2: Retry succeeds after EOS ------");
        // Create Pipeline
        ComPtr<IApp> app = App::Create();

        ComPtr<IPipeline> pipeline;
        ASSERT_S(CreatePipeline(pipeline.AddressOf(), "id", "videotestsrc num-buffers=2 ! fakesink", app));

        // Create PipelineErrorCollection
        ComPtr<IPipelineErrorCollection> collection;
        CreatePipelineErrorCollection(collection.AddressOf());
        ComPtr<IPipelineError> errorDef;
        CreatePipelineError(errorDef.AddressOf(), GST_MESSAGE_EOS, int(ErrorDomain::NOT_DEFINED), 0);
        collection->Add(errorDef);

        // Create retry mechanism
        ComPtr<IPipelineEventHandler> retryMechanism;
        ASSERT_S(CreateRetryMechanism(retryMechanism.AddressOf(), RetryMode::Linear, 0, 2000, 100, collection));
        ASSERT_S(pipeline->AddPipelineEventHandler(retryMechanism));
        
        // Create TestPipelineEventHandler
        ComPtr<TestPipelineEventHandler> pipelineEventHandler;
        ASSERT_S(TestPipelineEventHandler::Create(pipelineEventHandler.AddressOf()));
        bool afterEOS = false;
        pipelineEventHandler->SetOnError([&](IPipeline* sender, IPipelineError* error)
        {
            auto type = error->MessageType();
            if(error->MessageType() == static_cast<int32_t>(GST_MESSAGE_EOS))
            {
                afterEOS = true;
            }
        });
        ManualResetEvent playingEvt;
        pipelineEventHandler->SetOnStateChanged([&](IPipeline* sender, int32_t oldState, int32_t newState)
        {
            if(newState == static_cast<int32_t>(GST_STATE_PLAYING) && afterEOS)
            {
                playingEvt.Set();
            }
        });
        ASSERT_S(pipeline->AddPipelineEventHandler(pipelineEventHandler));
        ASSERT_S(pipeline->Start());

        ASSERT_TRUE(playingEvt.WaitFor(3000));
        pipeline->RemovePipelineEventHandler(pipelineEventHandler);
        pipeline->RemovePipelineEventHandler(retryMechanism);
    }
    {
        TraceInfo("------ Test 3: Retry failed after Error ------");
        // Create Pipeline
        ComPtr<IApp> app = App::Create();
        ComPtr<IPipeline> pipeline;
        ASSERT_S(CreatePipeline(pipeline.AddressOf(), "id", "filesrc location=/nonexistent/file ! fakesink", app));

        // Create PipelineErrorCollection
        ComPtr<IPipelineErrorCollection> collection;
        CreatePipelineErrorCollection(collection.AddressOf());
        ComPtr<IPipelineError> errorDef;
        CreatePipelineError(errorDef.AddressOf(), GST_MESSAGE_ERROR, int(ErrorDomain::CORE), GST_CORE_ERROR_FAILED);
        collection->Add(errorDef);

        // Create retry mechanism
        ComPtr<IPipelineEventHandler> retryMechanism;
        ASSERT_S(CreateRetryMechanism(retryMechanism.AddressOf(), RetryMode::Linear, 0, 500, 100, collection));
        ASSERT_S(pipeline->AddPipelineEventHandler(retryMechanism));

        // Create TestPipelineEventHandler
        ComPtr<TestPipelineEventHandler> pipelineEventHandler;
        ASSERT_S(TestPipelineEventHandler::Create(pipelineEventHandler.AddressOf()));
        bool afterError = false;
        bool retrySucceed = false;
        pipelineEventHandler->SetOnError([&](IPipeline* sender, IPipelineError* error)
        {
            afterError = true;
        });
        ManualResetEvent playingEvt;
        pipelineEventHandler->SetOnStateChanged([&](IPipeline* sender, int32_t oldState, int32_t newState)
        {
            if(newState == static_cast<int32_t>(GST_STATE_PLAYING) && afterError)
            {
                retrySucceed = true;
                playingEvt.Set();
            }
        });
        ASSERT_S(pipeline->AddPipelineEventHandler(pipelineEventHandler));
        pipeline->Start();

        ASSERT_FALSE(playingEvt.WaitFor(2000));
        ASSERT_EQ(afterError, true);
        ASSERT_EQ(retrySucceed, false);
        pipeline->RemovePipelineEventHandler(pipelineEventHandler);
        pipeline->RemovePipelineEventHandler(retryMechanism);
    }
} 