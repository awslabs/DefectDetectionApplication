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
#include <core/property/property_manager.h>

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
        COM_DTOR_FIN(TestPipelineEventHandler);
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

bool TimeOut(std::function<bool()> check, int32_t timeOutInMs)
{
    int count = 0;
    do
    { 
        if(check())
        {
            return false;
        }

        ThreadSleep(10);
        count += 10;
    } while(count < timeOutInMs);

    return true;
}

bool WaitForState(IPipeline* pipeline, GstState state, int timeOutInMs)
{
    while((GstState)(pipeline->State()) != state && timeOutInMs > 0)
    {
        timeOutInMs -= 10;
        ThreadSleep(10);
    }

    return timeOutInMs > 0;
}

class TestPropertyDelegate : public UnknownImpl<IPropertyDelegate>
{
public:
    static HRESULT Create(TestPropertyDelegate** ppObj)
    {
        HRESULT hr = S_OK;
        CREATE_COM(TestPropertyDelegate, ptr);

        CHECKHR(PropertyManager::Create(ptr->_propertyManager.AddressOf(), "testPropertyDelegate"));
        *ppObj = ptr.Detach();
        return hr;
    }

    ~TestPropertyDelegate()
    {
        COM_DTOR_FIN(TestPropertyDelegate);
    }

    HRESULT GetProperty(IProperty** ppObj, const char* property) override
    {
        return _propertyManager->GetProperty(ppObj, property);
    }

    HRESULT SetProperty(const char* propName, const char* value)
    {
        return _propertyManager->SetProperty(propName, value);
    }

    HRESULT SetBatch(nlohmann::json& json)
    {
        return _propertyManager->SetBatchProperty(nullptr, json);
    }

    HRESULT Synchronize(IPropertyCollection** ppObj) override
    {
        return CreatePropertyCollection(ppObj);
    }

private:
    ComPtr<PropertyManager> _propertyManager;
};

void PipelineBasicTests()
{
    HRESULT hr = S_OK;
    GstStateChangeReturn state_result;
    GstState state, pending_state;

    ComPtr<IApp> app = App::Create();

    // initilaize pipeline
    ComPtr<IPipeline> pipeline;
    ASSERT_EQ(CreatePipeline(pipeline.AddressOf(), "id", "videotestsrc ! fakesink", app), S_OK);

    // Verify pipeline state
    state_result = gst_element_get_state(pipeline->Element(), &state, &pending_state, GST_CLOCK_TIME_NONE);
    ASSERT_EQ(state_result, GST_STATE_CHANGE_SUCCESS);
    ASSERT_EQ(state, GST_STATE_NULL);
    ASSERT_EQ(pending_state, GST_STATE_VOID_PENDING);
    ASSERT_EQ(state, (GstState)(pipeline->State()));
    
    // start to play pipeline
    ASSERT_EQ(pipeline->Start(), S_OK);

    // Verify pipeline state
    state_result = gst_element_get_state(pipeline->Element(), &state, &pending_state, GST_CLOCK_TIME_NONE);
    ASSERT_EQ(state_result, GST_STATE_CHANGE_SUCCESS);
    ASSERT_EQ(state, GST_STATE_PLAYING);
    ASSERT_EQ(pending_state, GST_STATE_VOID_PENDING);
    ASSERT_EQ(state, (GstState)(pipeline->State()));

    // pause pipeline
    ASSERT_EQ(pipeline->Pause(), S_OK);

    // Verify pipeline state
    state_result = gst_element_get_state(pipeline->Element(), &state, &pending_state, GST_CLOCK_TIME_NONE);
    ASSERT_EQ(state_result, GST_STATE_CHANGE_SUCCESS);
    ASSERT_EQ(state, GST_STATE_PAUSED);
    ASSERT_EQ(pending_state, GST_STATE_VOID_PENDING);
    ASSERT_EQ(state, (GstState)(pipeline->State()));

    // start to play pipeline
    ASSERT_EQ(pipeline->SetState(GST_STATE_PLAYING, GST_STATE_PLAYING), S_OK);

    // Verify pipeline state
    state_result = gst_element_get_state(pipeline->Element(), &state, &pending_state, GST_CLOCK_TIME_NONE);
    ASSERT_EQ(state_result, GST_STATE_CHANGE_SUCCESS);
    ASSERT_EQ(state, GST_STATE_PLAYING);
    ASSERT_EQ(pending_state, GST_STATE_VOID_PENDING);
    ASSERT_EQ(state, (GstState)(pipeline->State()));

    // stop pipeline
    ASSERT_EQ(pipeline->Stop(), S_OK);

    // Verify pipeline state
    state_result = gst_element_get_state(pipeline->Element(), &state, &pending_state, GST_CLOCK_TIME_NONE);
    ASSERT_EQ(state_result, GST_STATE_CHANGE_SUCCESS);
    ASSERT_EQ(state, GST_STATE_NULL);
    ASSERT_EQ(pending_state, GST_STATE_VOID_PENDING);
    ASSERT_EQ(state, (GstState)(pipeline->State()));
}

void PipelineErrors()
{
    HRESULT hr = S_OK;

    ComPtr<IApp> app = App::Create();
    ComPtr<TestPipelineEventHandler> pipelineEventHandler;
    ASSERT_S(TestPipelineEventHandler::Create(pipelineEventHandler.AddressOf()));

    AutoResetEvent error_evt;
    pipelineEventHandler->SetOnError([&](IPipeline* sender, IPipelineError* error)
    {
        switch(error->MessageType())
        {
            case static_cast<int32_t>(GST_MESSAGE_ERROR):
                ASSERT_EQ(strcmp(error->ErrorMessage(), "Error Message"), 0);
                ASSERT_EQ(strcmp(error->DebugInfo(), "This is a fake error"), 0);
                ASSERT_EQ(strcmp(error->DomainAsString(), "gst-core-error-quark"), 0);
                ASSERT_EQ(error->Domain(), ErrorDomain::CORE);
                ASSERT_EQ(error->DomainQuark(), gst_core_error_quark());
                ASSERT_EQ(strcmp(error->ElementName(), "errorTestPipeline"), 0);
                ASSERT_EQ(strcmp(error->ElementFactory(), "pipeline"), 0);
                error_evt.Set();
                break;
            case static_cast<int32_t>(GST_MESSAGE_WARNING):
                ASSERT_EQ(strcmp(error->ErrorMessage(), "Warning Message"), 0);
                ASSERT_EQ(strcmp(error->DebugInfo(), "This is a fake warning"), 0);
                ASSERT_EQ(strcmp(error->DomainAsString(), "gst-resource-error-quark"), 0);
                ASSERT_EQ(error->Domain(), ErrorDomain::RESOURCE);
                ASSERT_EQ(error->DomainQuark(), gst_resource_error_quark());
                ASSERT_EQ(strcmp(error->ElementName(), "errorTestPipeline"), 0);
                ASSERT_EQ(strcmp(error->ElementFactory(), "pipeline"), 0);
                error_evt.Set();
                break;
            case static_cast<int32_t>(GST_MESSAGE_EOS):
                ASSERT_EQ(strcmp(error->ErrorMessage(), "End of Stream"), 0);
                ASSERT_EQ(strcmp(error->DebugInfo(), ""), 0);
                ASSERT_EQ(strcmp(error->DomainAsString(), ""), 0);
                ASSERT_EQ(error->Domain(), ErrorDomain::NOT_DEFINED);
                ASSERT_EQ(error->DomainQuark(), 0);
                ASSERT_EQ(strcmp(error->ElementName(), "errorTestPipeline"), 0);
                ASSERT_EQ(strcmp(error->ElementFactory(), "pipeline"), 0);
                error_evt.Set();
                break;
        }
    });

    // define pipeline
    ComPtr<IPipeline> pipeline;
    ASSERT_EQ(CreatePipeline(pipeline.AddressOf(), "errorTestPipeline", "videotestsrc ! fakesink", app), S_OK);
    ASSERT_EQ(pipeline->Start(), S_OK);
    ASSERT_S(pipeline->AddPipelineEventHandler(pipelineEventHandler));

    // post an error to bus
    {
        GstMessage *msg;
        GstBus *bus = gst_pipeline_get_bus(GST_PIPELINE(pipeline->Element()));
        GError *error = g_error_new(GST_CORE_ERROR, GST_CORE_ERROR_FAILED, "Error Message");
        msg = gst_message_new_error(GST_OBJECT(pipeline->Element()), error, "This is a fake error");
        gst_bus_post(bus, msg);
        g_clear_error(&error);
        gst_object_unref(bus);
    }

    ASSERT_TRUE(error_evt.WaitFor(3000));

    // post an error to bus
    {
        GstMessage *msg;
        GstBus *bus = gst_pipeline_get_bus(GST_PIPELINE(pipeline->Element()));
        GError *error = g_error_new(GST_RESOURCE_ERROR, GST_RESOURCE_ERROR_FAILED, "Warning Message");
        msg = gst_message_new_warning(GST_OBJECT(pipeline->Element()), error, "This is a fake warning");
        gst_bus_post(bus, msg);
        g_clear_error(&error);
        gst_object_unref(bus);
    }

    ASSERT_TRUE(error_evt.WaitFor(3000));

    // post an error to bus
    {
        GstMessage *msg;
        GstBus *bus = gst_pipeline_get_bus(GST_PIPELINE(pipeline->Element()));
        msg = gst_message_new_eos(GST_OBJECT(pipeline->Element()));
        gst_bus_post(bus, msg);
        gst_object_unref(bus);
    }

    ASSERT_TRUE(error_evt.WaitFor(3000));

    ASSERT_EQ(pipeline->Stop(), S_OK);
    ASSERT_EQ(GST_STATE_NULL, (GstState)(pipeline->State()));
}

void EOS()
{
    HRESULT hr = S_OK;
    ComPtr<IApp> app = App::Create();

    ComPtr<TestPipelineEventHandler> eventHandler;
    TestPipelineEventHandler::Create(eventHandler.AddressOf());

    ManualResetEvent evt1;
    ManualResetEvent evt2;
    eventHandler->SetOnError([&](IPipeline* sender, IPipelineError* error)
    {
        ASSERT_EQ(error->MessageType(), static_cast<int32_t>(GST_MESSAGE_EOS));
        if(strcmp(sender->Id(), "id1") == 0)
        {
            evt1.Set();
        }
        else if(strcmp(sender->Id(), "id2") == 0)
        {
            evt2.Set();
        }
    });

    ComPtr<IPipelineDefinition> pipeline_definition, pipeline_definition2;
    ComPtr<IPipeline> pipeline, pipeline2;

    // create pipeline1
    ASSERT_S(CreatePipeline(pipeline.AddressOf(), "id1", "videotestsrc num-buffers=5 ! fakesink", app));

    // create pipeline2
    ASSERT_S(CreatePipeline(pipeline2.AddressOf(), "id2", "videotestsrc num-buffers=5 ! fakesink", app));

    pipeline->AddPipelineEventHandler(eventHandler);
    pipeline2->AddPipelineEventHandler(eventHandler);

    // start pipelines
    ASSERT_EQ(pipeline->Start(), S_OK);
    ASSERT_EQ(pipeline2->Start(), S_OK);

    ASSERT_TRUE(evt1.WaitFor(3000));
    ASSERT_TRUE(evt2.WaitFor(3000));
}

void RestartTests()
{
    HRESULT hr = S_OK;

    int argc;
    char** argv;

    GStreamer::Initialize(2);
    
    CommandLineArgs args = CreateCommandLineArgs("exeName --PATTERN {\"type\":\"string\", \"value\":\"ball\"} --GAMMA_DECODE {\"type\":\"string\", \"value\":\"true\"}");
    ComPtr<IApp> app = App::CreateWithArgs(args.Count(), args.Values());

    {
        GstStateChangeReturn state_result;
        GstState state, pending_state;

        // define pipeline
        ComPtr<IPipeline> pipeline;
        ASSERT_EQ(CreatePipeline(pipeline.AddressOf(), "id", "videotestsrc ! fakesink", app), S_OK);

        // Start Pipeline
        ASSERT_EQ(pipeline->Start(), S_OK);
    
        // Verify pipeline state
        state_result = gst_element_get_state(pipeline->Element(), &state, &pending_state, GST_CLOCK_TIME_NONE);
        ASSERT_EQ(state_result, GST_STATE_CHANGE_SUCCESS);
        ASSERT_EQ(state, GST_STATE_PLAYING);
        ASSERT_EQ(pending_state, GST_STATE_VOID_PENDING);


        // Restart Pipeline
        ASSERT_EQ(pipeline->Restart(), S_OK);

        // Verify pipeline state
        state_result = gst_element_get_state(pipeline->Element(), &state, &pending_state, GST_CLOCK_TIME_NONE);
        ASSERT_EQ(state_result, GST_STATE_CHANGE_SUCCESS);
        ASSERT_EQ(state, GST_STATE_PLAYING);
        ASSERT_EQ(pending_state, GST_STATE_VOID_PENDING);

        // Restart Pipeline with new definition
        std::string new_def = "videotestsrc pattern = 0 ! fakesink";

        ASSERT_EQ(pipeline->ChangeDefinition(new_def.c_str()), S_OK);
        // Verify pipeline definition is changed
        ASSERT_EQ(strcmp(pipeline->Definition(), new_def.c_str()), 0);
        // Verify pipeline state
        state_result = gst_element_get_state(pipeline->Element(), &state, &pending_state, GST_CLOCK_TIME_NONE);
        ASSERT_EQ(state_result, GST_STATE_CHANGE_SUCCESS);
        ASSERT_EQ(state, GST_STATE_PLAYING);
        ASSERT_EQ(pending_state, GST_STATE_VOID_PENDING);

        // Toggle back and forth between having dynamic variables and not
        new_def = "videotestsrc name=src pattern=${PATTERN} ! fakesink";
        pipeline->ChangeDefinition(new_def.c_str());
        ASSERT_EQ(strcmp(pipeline->Definition(), new_def.c_str()), 0);
        state_result = gst_element_get_state(pipeline->Element(), &state, &pending_state, GST_CLOCK_TIME_NONE);
        ASSERT_EQ(state, GST_STATE_PLAYING);
        ASSERT_EQ(pending_state, GST_STATE_VOID_PENDING);

        new_def = "videotestsrc name=src pattern=0 ! fakesink";
        pipeline->ChangeDefinition(new_def.c_str());
        ASSERT_EQ(strcmp(pipeline->Definition(), new_def.c_str()), 0);
        state_result = gst_element_get_state(pipeline->Element(), &state, &pending_state, GST_CLOCK_TIME_NONE);
        ASSERT_EQ(state, GST_STATE_PLAYING);
        ASSERT_EQ(pending_state, GST_STATE_VOID_PENDING);

        new_def = "videotestsrc name=src pattern=${PATTERN} ! fakesink";
        pipeline->ChangeDefinition(new_def.c_str());
        ASSERT_EQ(strcmp(pipeline->Definition(), new_def.c_str()), 0);
        state_result = gst_element_get_state(pipeline->Element(), &state, &pending_state, GST_CLOCK_TIME_NONE);
        ASSERT_EQ(state, GST_STATE_PLAYING);
        ASSERT_EQ(pending_state, GST_STATE_VOID_PENDING);
    }
}

void PipelineCornerCaseTests()
{
    HRESULT hr = S_OK;
    GStreamer::Initialize(2);
    ComPtr<IApp> app = App::Create();

    // initilaize pipeline
    ComPtr<IPipeline> pipeline;
    ASSERT_S(CreatePipeline(pipeline.AddressOf(), "id", "videotestsrc ! \"video/x-raw,width=640,height=480\" ! videoconvert ! capsfilter \"caps=video/x-raw,format=I420\" ! videorate ! capsfilter \"caps=video/x-raw,framerate=30/1\" ! autovideosink", app));
}

void PipelineErrorCollectionTests()
{
    HRESULT hr = S_OK;
    GStreamer::Initialize(2);

    ComPtr<IPipelineErrorCollection> collection;
    CreatePipelineErrorCollection(collection.AddressOf());
    {
        ComPtr<IPipelineError> added_err_def;
        CreatePipelineError(added_err_def.AddressOf(), GST_MESSAGE_ERROR, int(ErrorDomain::CORE), GST_CORE_ERROR_FAILED);
        collection->Add(added_err_def);

        ComPtr<IPipelineError> error_def;
        CreatePipelineError(error_def.AddressOf(), GST_MESSAGE_ERROR, int(ErrorDomain::CORE), GST_CORE_ERROR_FAILED);
        ASSERT_EQ(collection->Contains(error_def), true);

        ComPtr<IPipelineError> warning_def;
        CreatePipelineError(warning_def.AddressOf(), GST_MESSAGE_WARNING, int(ErrorDomain::CORE), GST_CORE_ERROR_FAILED);
        ASSERT_EQ(collection->Contains(warning_def), false);

        ComPtr<IPipelineError> eos_def;
        CreatePipelineError(eos_def.AddressOf(), GST_MESSAGE_EOS, int(ErrorDomain::NOT_DEFINED), 0);
        ASSERT_EQ(collection->Contains(eos_def), false);

        collection->Remove(added_err_def);
    }

    {
        ComPtr<IPipelineError> added_err_def;
        CreatePipelineError(added_err_def.AddressOf(), ALL, int(ErrorDomain::CORE), GST_CORE_ERROR_FAILED);
        collection->Add(added_err_def);

        ComPtr<IPipelineError> error_def;
        CreatePipelineError(error_def.AddressOf(), GST_MESSAGE_ERROR, int(ErrorDomain::RESOURCE), GST_RESOURCE_ERROR_FAILED);
        ASSERT_EQ(collection->Contains(error_def), false);

        ComPtr<IPipelineError> warning_def;
        CreatePipelineError(warning_def.AddressOf(), GST_MESSAGE_WARNING, int(ErrorDomain::CORE), GST_CORE_ERROR_FAILED);
        ASSERT_EQ(collection->Contains(warning_def), true);
        
        ComPtr<IPipelineError> eos_def;
        CreatePipelineError(eos_def.AddressOf(), GST_MESSAGE_EOS, int(ErrorDomain::NOT_DEFINED), 0);
        ASSERT_EQ(collection->Contains(eos_def), true);
        
        collection->Remove(added_err_def);
    }
    {
        // test all error domain + specific error code
        // same error code in different domain means different error
        ComPtr<IPipelineError> added_err_def;
        CreatePipelineError(added_err_def.AddressOf(), ALL, ALL, GST_CORE_ERROR_CAPS);
        collection->Add(added_err_def);
        ASSERT_EQ(GST_CORE_ERROR_CAPS, 10);
        ASSERT_EQ(GST_RESOURCE_ERROR_WRITE, 10);

        ComPtr<IPipelineError> error_def;
        CreatePipelineError(error_def.AddressOf(), GST_MESSAGE_ERROR, int(ErrorDomain::RESOURCE), GST_RESOURCE_ERROR_WRITE);
        ASSERT_EQ(collection->Contains(error_def), true);

        ComPtr<IPipelineError> warning_def;
        CreatePipelineError(warning_def.AddressOf(), GST_MESSAGE_WARNING, int(ErrorDomain::CORE), GST_CORE_ERROR_FAILED);
        ASSERT_EQ(collection->Contains(warning_def), false);

        ComPtr<IPipelineError> eos_def;
        CreatePipelineError(eos_def.AddressOf(), GST_MESSAGE_EOS, int(ErrorDomain::NOT_DEFINED), 0);
        ASSERT_EQ(collection->Contains(eos_def), true);
        
        collection->Remove(added_err_def);
    }
}

void PropertyChangedTest()
{
    HRESULT hr = S_OK;

    int argc;
    char** argv;
    {
        TraceInfo("-------- Test 1 -------");
        CommandLineArgs args = CreateCommandLineArgs("exeName --PATTERN {\"type\":\"string\",\"value\":\"1\"}");
        ComPtr<IApp> app = App::CreateWithArgs(args.Count(), args.Values());

        ComPtr<IPipeline> pipeline;
        ASSERT_S(CreatePipeline(pipeline.AddressOf(), "id", "videotestsrc name=src pattern=${PATTERN} ! fakesink", app));

        // Validate property pattern is set correctly
        GstElement* elem = gst_bin_get_by_name(GST_BIN(pipeline->Element()), "src");
        ASSERT_FALSE(elem == nullptr);
        gint pattern;
        g_object_get(G_OBJECT(elem), "pattern", &pattern, NULL);
        ASSERT_EQ(1, pattern);
        gst_object_unref(elem);

        // Change the property and refresh the pipeline
        ComPtr<IStringProperty> property;
        ASSERT_S(app->GetStringProperty(property.AddressOf(), "PATTERN"));
        ASSERT_S(property->Set("{\"type\":\"string\",\"value\":\"0\"}"));
        ASSERT_S(pipeline->Refresh());

        // Validate the gstreamer element has had the property updated
        elem = gst_bin_get_by_name(GST_BIN(pipeline->Element()), "src");
        ASSERT_FALSE(elem == nullptr);
        bool timedOut = TimeOut([&]()
        {
            gint currentPattern;
            g_object_get(G_OBJECT(elem), "pattern", &currentPattern, NULL);
            return currentPattern == 0;
        }, 1000);
        ASSERT_FALSE(timedOut);
        gst_object_unref(elem);
    }

    {
        TraceInfo("-------- Test 2 -------");
        // Create TestPipelineEventHandler
        ComPtr<TestPipelineEventHandler> pipelineEventHandler;
        ASSERT_S(TestPipelineEventHandler::Create(pipelineEventHandler.AddressOf()));

        AutoResetEvent playingEvt;
        pipelineEventHandler->SetOnStateChanged([&](IPipeline* sender, int32_t oldState, int32_t newState)
        {
            if(newState == static_cast<int32_t>(GST_STATE_PLAYING))
            {
                playingEvt.Set();
            }
        });

        CommandLineArgs args = CreateCommandLineArgs("exeName --PATTERN {\"type\":\"string\",\"value\":\"1\",\"immutable\":true}");
        ComPtr<IApp> app = App::CreateWithArgs(args.Count(), args.Values());

        ComPtr<IPipeline> pipeline;
        ASSERT_S(CreatePipeline(pipeline.AddressOf(), "testDefinition", "videotestsrc name=src pattern=${PATTERN} ! fakesink", app));
        ASSERT_S(pipeline->AddPipelineEventHandler(pipelineEventHandler));

        // Validate property pattern is set correctly
        GstElement* elem = gst_bin_get_by_name(GST_BIN(pipeline->Element()), "src");
        ASSERT_FALSE(elem == nullptr);
        gint pattern;
        g_object_get(G_OBJECT(elem), "pattern", &pattern, NULL);
        ASSERT_EQ(1, pattern);
        gst_object_unref(elem);

        ComPtr<IStringProperty> property;
        ASSERT_S(app->GetStringProperty(property.AddressOf(), "PATTERN"));
        ASSERT_S(property->Set("{\"type\":\"string\",\"value\":\"0\",\"immutable\":true}"));

        ASSERT_S(pipeline->Refresh());
        ASSERT_TRUE(playingEvt.WaitFor(3000));

        elem = gst_bin_get_by_name(GST_BIN(pipeline->Element()), "src");
        ASSERT_FALSE(elem == nullptr);
        g_object_get(G_OBJECT(elem), "pattern", &pattern, NULL);
        ASSERT_EQ(0, pattern);
        gst_object_unref(elem);
    }

    {
        // Change an immutable string property with two defined in the pipeline
        TraceInfo("-------- Test 3 -------");
        // Create TestPipelineEventHandler
        ComPtr<TestPipelineEventHandler> pipelineEventHandler;
        ASSERT_S(TestPipelineEventHandler::Create(pipelineEventHandler.AddressOf()));

        AutoResetEvent playingEvt;
        pipelineEventHandler->SetOnStateChanged([&](IPipeline* sender, int32_t oldState, int32_t newState)
        {
            if(newState == static_cast<int32_t>(GST_STATE_PLAYING))
            {
                playingEvt.Set();
            }
        });

        CommandLineArgs args = CreateCommandLineArgs("exeName --PATTERN {\"type\":\"string\",\"value\":\"1\",\"immutable\":true} --TEXT {\"type\":\"string\",\"value\":\"hello\",\"immutable\":true}");
        ComPtr<IApp> app = App::CreateWithArgs(args.Count(), args.Values());

        ComPtr<IPipeline> pipeline;
        ASSERT_S(CreatePipeline(pipeline.AddressOf(), "testDefinition", "videotestsrc name=src pattern=${PATTERN} ! textoverlay name=overlay text=\"${TEXT}\" ! fakesink", app));
        ASSERT_S(pipeline->AddPipelineEventHandler(pipelineEventHandler));

        ComPtr<IStringProperty> property;
        ASSERT_S(app->GetStringProperty(property.AddressOf(), "PATTERN"));
        ASSERT_S(property->Set("{\"type\":\"string\",\"value\":\"0\",\"immutable\":true}"));
        ASSERT_S(pipeline->Refresh());
        ASSERT_TRUE(playingEvt.WaitFor(3000));
    }

    {
        TraceInfo("-------- Test 4 -------");

        // Create TestPipelineEventHandler
        ComPtr<TestPipelineEventHandler> pipelineEventHandler;
        ASSERT_S(TestPipelineEventHandler::Create(pipelineEventHandler.AddressOf()));

        AutoResetEvent playingEvt;
        pipelineEventHandler->SetOnStateChanged([&](IPipeline* sender, int32_t oldState, int32_t newState)
        {
            if(newState == static_cast<int32_t>(GST_STATE_PLAYING))
            {
                playingEvt.Set();
            }
        });

        // Change 2 immutable string property with two defined in the pipeline
        ComPtr<TestPropertyDelegate> propDelegate;
        TestPropertyDelegate::Create(propDelegate.AddressOf());
        ASSERT_S(propDelegate->SetProperty("PATTERN", "{\"type\":\"string\",\"value\":\"1\",\"immutable\":true}"));
        ASSERT_S(propDelegate->SetProperty("TEXT", "{\"type\":\"string\",\"value\":\"hello\",\"immutable\":true}"));
        ComPtr<IApp> app = App::Create();
        ASSERT_S(app->AddPropertyDelegate(propDelegate));

        // Create a pipeline from definition
        ComPtr<IPipeline> pipeline;
        ASSERT_S(CreatePipeline(pipeline.AddressOf(), "testDefinition", "videotestsrc name=src pattern=${PATTERN} ! textoverlay name=overlay text=\"${TEXT}\" ! fakesink", app));
        ASSERT_S(pipeline->AddPipelineEventHandler(pipelineEventHandler));

        // Update the property of pattern and validate the new property has been set
        nlohmann::json json;
        json["PATTERN"] = "{\"type\":\"string\",\"value\":\"0\",\"immutable\":true}";
        json["TEXT"] = "{\"type\":\"string\",\"value\":\"hello 2\",\"immutable\":true}";
        propDelegate->SetBatch(json);

        ASSERT_S(pipeline->Refresh());
        EXPECT_TRUE(playingEvt.WaitFor(3000));
    }

    {
        TraceInfo("-------- Test 5 -------");
        // Create TestPipelineEventHandler
        ComPtr<TestPipelineEventHandler> pipelineEventHandler;
        ASSERT_S(TestPipelineEventHandler::Create(pipelineEventHandler.AddressOf()));

        AutoResetEvent playingEvt;
        pipelineEventHandler->SetOnStateChanged([&](IPipeline* sender, int32_t oldState, int32_t newState)
        {
            if(newState == static_cast<int32_t>(GST_STATE_PLAYING))
            {
                playingEvt.Set();
            }
        });

        // Setup variables
        ComPtr<TestPropertyDelegate> propDelegate;
        TestPropertyDelegate::Create(propDelegate.AddressOf());
        ASSERT_S(propDelegate->SetProperty("PATTERN", "{\"type\":\"string\", \"value\":\"0\"}"));
        ASSERT_S(propDelegate->SetProperty("UNUSED", "{\"type\":\"string\", \"value\":\"0\"}"));
        ComPtr<IApp> app = App::Create();
        ASSERT_S(app->AddPropertyDelegate(propDelegate));

        // Create a pipeline from definition
        ComPtr<IPipeline> pipeline;
        ASSERT_S(CreatePipeline(pipeline.AddressOf(), "test", "videotestsrc name=src pattern=${PATTERN} ! fakesink", app));
        ASSERT_S(pipeline->AddPipelineEventHandler(pipelineEventHandler));

        ASSERT_S(pipeline->Start());
        EXPECT_TRUE(playingEvt.WaitFor(3000));

         // Change the definition
        pipeline->ChangeDefinition("videotestsrc name=anotherSrc pattern=${PATTERN} ! fakesink");
        EXPECT_TRUE(playingEvt.WaitFor(3000));

        // Stop the pipeline
        ASSERT_S(pipeline->Stop());
        ASSERT_TRUE(WaitForState(pipeline, GST_STATE_NULL, 3000));
    }
}

void BlockingElementsTest()
{
    HRESULT hr = S_OK;

    ComPtr<IApp> app = App::Create();
    ASSERT_TRUE(app != nullptr);

    ComPtr<IPipeline> pipeline;
    ASSERT_S(CreatePipeline(pipeline.AddressOf(), "id", "videotestsrc ! gate open=false ! fakesink", app));
    ASSERT_S(pipeline->Start());

    // Stop in a background thread so we can check if starting a pipeline with a blocking element
    // prevents stopping
    ManualResetEvent stopped_state;
    std::thread t = std::thread([&]()
    {
        pipeline->Stop();
        stopped_state.Set();
    });

    // FYI. If this assertion fails then you will see the "std::terminate without an active exception error, that is OK"
    ASSERT_TRUE(stopped_state.WaitFor(3000));
    if(t.joinable())
    {
        t.join();
    }
}

TEST(GstTests, PipelineTests)
{
    SetEnvVar("GST_PLUGIN_PATH", BuildDirectory()+"/lib");
    GStreamer::Initialize(2);

    PipelineBasicTests();
    PipelineErrors();
    PropertyChangedTest();
    EOS();
    PipelineErrorCollectionTests();
    RestartTests();
    BlockingElementsTest();
    PipelineCornerCaseTests();

    GStreamer::Shutdown();
}