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
#define PIPELINE_DEFINITIONS_KEY "pipelines"

class TestPipelineManagerEventHandler : public UnknownImpl<IPipelineManagerEventHandler>
{
public:
    static HRESULT Create(TestPipelineManagerEventHandler** ppObj)
    {
        HRESULT hr = S_OK;
        CREATE_COM(TestPipelineManagerEventHandler, ptr);
        *ppObj = ptr.Detach();
        return hr;
    }

    ~TestPipelineManagerEventHandler()
    {
        COM_DTOR_FIN(TestPipelineManagerEventHandler);
    }

    bool OnPipelineAddPreview(IPipelineDefinition* definition) override
    {
        if(_OnPipelineAddPreviewCb)
        {
            return _OnPipelineAddPreviewCb(definition);
        }

        return false;
    }

    bool OnPipelineRemovePreview(IPipeline* pipeline) override
    {
        if(_OnPipelineRemovePreviewCb)
        {
            return _OnPipelineRemovePreviewCb(pipeline);
        }

        return false;
    }

    void OnPipelineAdded(IPipeline* pipeline) override
    {
        if(_OnPipelineAddedCb)
        {
            _OnPipelineAddedCb(pipeline);
        }
    }

    void OnPipelineRemoved(IPipeline* pipeline) override
    {
        if(_OnPipelineRemovedCb)
        {
            _OnPipelineRemovedCb(pipeline);
        }
    }

    bool OnDefintionChangePreview(IPipeline* sender, const char* newDefinition) override
    {
        if(_onDefinitionChangePreviewCb)
        {
            return _onDefinitionChangePreviewCb(sender, newDefinition);
        }

        return false;
    }

    void SetPipelineAddPreview(std::function<bool(IPipelineDefinition*)> cb)
    {
        _OnPipelineAddPreviewCb = std::move(cb);
    }

    void SetOnPipelineRemovePreview(std::function<bool(IPipeline*)> cb)
    {
        _OnPipelineRemovePreviewCb = std::move(cb);
    }

    void SetOnPipelineAddedCb(std::function<void(IPipeline*)> cb)
    {
        _OnPipelineAddedCb = std::move(cb);
    }

    void SetOnPipelineRemovedCb(std::function<void(IPipeline*)> cb)
    {
        _OnPipelineRemovedCb =  std::move(cb);
    }

    void SetOnDefintionChangePreview(std::function<bool(IPipeline*, const char*)> cb)
    {
        _onDefinitionChangePreviewCb =  std::move(cb);
    }

    std::function<bool(IPipelineDefinition*)> _OnPipelineAddPreviewCb;
    std::function<bool(IPipeline*)> _OnPipelineRemovePreviewCb;
    std::function<void(IPipeline*)> _OnPipelineAddedCb;
    std::function<void(IPipeline*)> _OnPipelineRemovedCb;
    std::function<bool(IPipeline*, const char*)> _onDefinitionChangePreviewCb;
};

std::string GetStrDefinitionOfPipelines(const std::vector<std::pair<std::string, std::string>>& pipelines)
{
    std::stringstream pipelines_stream;
    pipelines_stream << "[";
    for(int idx=0; idx < pipelines.size(); idx++)
    {
        pipelines_stream << "{\"id\":\"" << pipelines[idx].first << "\",\"definition\":\"" << pipelines[idx].second << "\"}";
        if(idx != pipelines.size()-1)
        {
            pipelines_stream << ",";
        }
    }
    pipelines_stream << "]";
    return pipelines_stream.str();
}

void ChangePipelineProperty(IStringProperty* prop, std::vector<std::pair<std::string, std::string>> pipelines)
{
    std::string pipeline_str = std::move(GetStrDefinitionOfPipelines(pipelines));
    // Update Property 
    TraceInfo("Setting pipeline property to %s", pipeline_str.c_str());
    prop->Set(pipeline_str.c_str());
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

TEST(GstTests, PipelineManagerBasicTests)
{
    HRESULT hr = S_OK;
    GStreamer::Initialize(2);

    std::vector<std::pair<std::string, std::string>> pipelines = 
    {
        {"id1", "videotestsrc pattern=snow ! fakesink"},
        {"id2", "videotestsrc pattern=ball ! fakesink"}
    };

    int argc;
    char** argv;
    std::stringstream ss;
    ss << "exeName --pipelines " << GetStrDefinitionOfPipelines(pipelines);
    std::string cli = ss.str();

    // Initialize App 
    CommandLineArgs args = CreateCommandLineArgs(cli.c_str());
    ComPtr<IApp> app = App::CreateWithArgs(args.Count(), args.Values());

    // Create the pipeline manager event handler
    ComPtr<TestPipelineManagerEventHandler> eventHandler;
    ASSERT_S(TestPipelineManagerEventHandler::Create(eventHandler.AddressOf()));

    AutoResetEvent pipelineRemovePreviewEvt;
    eventHandler->SetOnPipelineRemovePreview([&](IPipeline* pipeline)
    {
        pipelineRemovePreviewEvt.Set();
        return false;
    });

    AutoResetEvent pipelineAddPreviewEvt;
    eventHandler->SetPipelineAddPreview([&](IPipelineDefinition* definition)
    {
        pipelineAddPreviewEvt.Set();
        return false;
    });

    AutoResetEvent pipelineAddedEvt;
    eventHandler->SetOnPipelineAddedCb([&](IPipeline* pipeline)
    {
        pipelineAddedEvt.Set();
    });

    AutoResetEvent pipelineRemovedEvt;
    eventHandler->SetOnPipelineRemovedCb([&](IPipeline* pipeline)
    {
        pipelineRemovedEvt.Set();
    });

    AutoResetEvent pipelineDefinitionPreviewEvt;
    eventHandler->SetOnDefintionChangePreview([&](IPipeline* pipeline, const char* definition)
    {
        pipelineDefinitionPreviewEvt.Set();
        return false;
    });

    // Get Property Value
    ComPtr<IStringProperty> appProp;
    ASSERT_S(app->GetStringProperty(appProp.AddressOf(), PIPELINE_DEFINITIONS_KEY));

    // Create PipelineManager
    ComPtr<IPipelineManager> pipelineMgr;
    ASSERT_S(CreatePipelineManager(pipelineMgr.AddressOf(), app));
    ASSERT_S(pipelineMgr->AddPipelineManagerEventHandler(eventHandler));
    ASSERT_S(pipelineMgr->Initialize());
    ASSERT_EQ(pipelineMgr->Count(), 2);
    ASSERT_TRUE(pipelineAddPreviewEvt.WaitFor(3000));
    ASSERT_TRUE(pipelineAddedEvt.WaitFor(0));

    // Get Pipeline
    for(int i = 0; i < pipelineMgr->Count(); i++)
    {
        ComPtr<IPipeline> pipeline;
        ASSERT_S(pipelineMgr->GetPipelineById(pipeline.AddressOf(), pipelines[i].first.c_str()));
        ASSERT_NE(pipeline, nullptr);
        ASSERT_EQ(pipeline->Definition(), pipelines[i].second);
    }
    
    // Start All
    ASSERT_S(pipelineMgr->Start());
    for(int i = 0; i < pipelineMgr->Count(); i++)
    {
        ComPtr<IPipeline> pipeline;
        ASSERT_S(pipelineMgr->GetPipelineById(pipeline.AddressOf(), pipelines[i].first.c_str()));
        ASSERT_NE(pipeline, nullptr);
        GstState state, pending_state;
        ASSERT_EQ(gst_element_get_state(pipeline->Element(), &state, &pending_state, GST_CLOCK_TIME_NONE), GST_STATE_CHANGE_SUCCESS);
        ASSERT_EQ(state, GST_STATE_PLAYING);
        ASSERT_EQ(pending_state, GST_STATE_VOID_PENDING);
    }

    // Test Default ConfigChanged Handler
    pipelines.pop_back();
    pipelines[0].second = "videotestsrc ! fakesink";
    pipelines.emplace_back("id3", "videotestsrc ! fakesink");
    pipelines.emplace_back("id4", "videotestsrc ! fakesink");
    ChangePipelineProperty(appProp, pipelines);

    ASSERT_S(pipelineMgr->Refresh());
    ASSERT_EQ(pipelineMgr->Count(), 3);
    ASSERT_TRUE(pipelineAddPreviewEvt.WaitFor(0));
    ASSERT_TRUE(pipelineAddedEvt.WaitFor(0));
    ASSERT_TRUE(pipelineRemovePreviewEvt.WaitFor(0));
    ASSERT_TRUE(pipelineRemovedEvt.WaitFor(0));
    ASSERT_TRUE(pipelineDefinitionPreviewEvt.WaitFor(0));

    for(int i = 0; i < pipelineMgr->Count(); i++)
    {
        ComPtr<IPipeline> pipeline;
        ASSERT_S(pipelineMgr->GetPipelineById(pipeline.AddressOf(), pipelines[i].first.c_str()));
        ASSERT_NE(pipeline, nullptr);
        ASSERT_EQ(pipeline->Definition(), pipelines[i].second);
    }

    // Test Custom ConfigChanged Handler
    pipelines.erase(pipelines.begin());
    pipelines[0].second = "videotestsrc pattern = snow ! fakesink";
    pipelines.emplace_back("id5", "videotestsrc ! fakesink");
    pipelines.emplace_back("id6", "videotestsrc ! fakesink");
    ChangePipelineProperty(appProp, pipelines);
    ASSERT_S(pipelineMgr->Refresh());
    ASSERT_EQ(pipelineMgr->Count(), 4);

    // Wait for pipelines[0] async restart job to call SetDefinition
    for(int i = 0; i < pipelineMgr->Count(); i++)
    {
        ComPtr<IPipeline> pipeline;
        ASSERT_S(pipelineMgr->GetPipelineById(pipeline.AddressOf(), pipelines[i].first.c_str()));
        ASSERT_NE(pipeline, nullptr);
        ASSERT_EQ(pipeline->Definition(), pipelines[i].second);
    }

    // Restart All
    ASSERT_S(pipelineMgr->Restart());
    for(int i = 0; i < pipelineMgr->Count(); i++)
    {
        ComPtr<IPipeline> pipeline;
        ASSERT_S(pipelineMgr->GetPipelineById(pipeline.AddressOf(), pipelines[i].first.c_str()));
        ASSERT_NE(pipeline, nullptr);
        GstState state, pending_state;
        ASSERT_EQ(gst_element_get_state(pipeline->Element(), &state, &pending_state, GST_CLOCK_TIME_NONE), GST_STATE_CHANGE_SUCCESS);
        ASSERT_EQ(state, GST_STATE_PLAYING);
        ASSERT_EQ(pending_state, GST_STATE_VOID_PENDING);
    }

    // Stop All
    ASSERT_S(pipelineMgr->Stop());
    for(int i = 0; i < pipelineMgr->Count(); i++)
    {
        ComPtr<IPipeline> pipeline;
        ASSERT_S(pipelineMgr->GetPipelineById(pipeline.AddressOf(), pipelines[i].first.c_str()));
        ASSERT_NE(pipeline, nullptr);
        ASSERT_EQ(GST_STATE_NULL, (GstState)(pipeline->State()));
    }
}

TEST(GstTests, PipelineManagerPipelineParsingTests)
{
    HRESULT hr = S_OK;
    int argc;
    char** argv;

    GStreamer::Initialize(2);

    {
        TraceInfo("------ Test 1 ------");
        // Valid pipelines
        CommandLineArgs args = CreateCommandLineArgs("exeName --pipelines [{\"id\":\"id1\",\"definition\":\"videotestsrc pattern=snow ! fakesink\"},{\"id\":\"id2\",\"definition\":\"videotestsrc pattern=ball ! fakesink\"}]");
        ComPtr<IApp> app = App::CreateWithArgs(args.Count(), args.Values());

        ComPtr<IPipelineManager> manager;
        ASSERT_S(CreatePipelineManager(manager.AddressOf(), app));
        ASSERT_S(manager->Initialize());
    }

    {
        TraceInfo("------ Test 2 ------");
        // 1 invalid pipelines
        CommandLineArgs args = CreateCommandLineArgs("exeName --pipelines [{\"id\":\"id1\",\"definition\":\"notaplugin ! fakesink\"},{\"id\":\"id2\",\"definition\":\"videotestsrc pattern=ball ! fakesink\"}]");
        ComPtr<IApp> app = App::CreateWithArgs(args.Count(), args.Values());

        ComPtr<IPipelineManager> manager;
        ASSERT_S(CreatePipelineManager(manager.AddressOf(), app));
        ASSERT_F(manager->Initialize());
    }

    {
        TraceInfo("------ Test 3 ------");
        // 2 invalid pipelines
        CommandLineArgs args = CreateCommandLineArgs("exeName --pipelines [{\"id\":\"id1\",\"definition\":\"notaplugin ! fakesink\"},{\"id\":\"id2\",\"definition\":\"stillnotaplugin ! fakesink\"}]");
        ComPtr<IApp> app = App::CreateWithArgs(args.Count(), args.Values());

        ComPtr<IPipelineManager> manager;
        ASSERT_S(CreatePipelineManager(manager.AddressOf(), app));
        ASSERT_F(manager->Initialize());
    }

    {
        TraceInfo("------ Test 4 ------");
        // Create valid pipeline, change it to invalid, then back to valid
        CommandLineArgs args = CreateCommandLineArgs("exeName --pipelines [{\"id\":\"id1\",\"definition\":\"videotestsrc pattern=snow ! fakesink\"}]");
        ComPtr<IApp> app = App::CreateWithArgs(args.Count(), args.Values());

        ComPtr<IPipelineManager> manager;
        ASSERT_S(CreatePipelineManager(manager.AddressOf(), app));
        ASSERT_S(manager->Initialize());
        ASSERT_S(manager->Start());

        // Validate pipeline is playing
        ComPtr<IPipeline> pipeline;
        manager->GetPipelineById(pipeline.AddressOf(), "id1");
        ASSERT_TRUE(GST_STATE_READY <= (GstState)(pipeline->State()));

        // set to an invalid pipeline
        ComPtr<IStringProperty> pipelineProperty;
        ASSERT_S(app->GetStringProperty(pipelineProperty.AddressOf(), "pipelines"));
        nlohmann::json j_obj = nlohmann::json::parse(pipelineProperty->Get());
        j_obj[0]["definition"] = "notaplugin ! fakesink";
        pipelineProperty->Set(j_obj.dump().c_str());
        ASSERT_F(manager->Refresh());

        // Reset to a valid pipeline
        j_obj[0]["definition"] = "videotestsrc pattern=ball ! fakesink";
        pipelineProperty->Set(j_obj.dump().c_str());
        ASSERT_S(manager->Refresh());
        ASSERT_TRUE(WaitForState(pipeline, GST_STATE_PLAYING, 3000));
    }

    {
        TraceInfo("------ Test 5 ------");
        // Create valid pipeline, change it to invalid, stop the pipeline, then try and play it
        CommandLineArgs args = CreateCommandLineArgs("exeName --pipelines [{\"id\":\"id1\",\"definition\":\"videotestsrc pattern=snow ! fakesink\"}]");
        ComPtr<IApp> app = App::CreateWithArgs(args.Count(), args.Values());

        ComPtr<IPipelineManager> manager;
        ASSERT_S(CreatePipelineManager(manager.AddressOf(), app));
        ASSERT_S(manager->Initialize());
        ASSERT_S(manager->Start());

        // Validate pipeline is playing
        ComPtr<IPipeline> pipeline;
        manager->GetPipelineById(pipeline.AddressOf(), "id1");
        ASSERT_TRUE(GST_STATE_READY <= (GstState)(pipeline->State()));

        // set to an invalid pipeline
        ComPtr<IStringProperty> pipelineProperty;
        ASSERT_S(app->GetStringProperty(pipelineProperty.AddressOf(), "pipelines"));
        nlohmann::json j_obj = nlohmann::json::parse(pipelineProperty->Get());
        j_obj[0]["definition"] = "notaplugin ! fakesink";
        pipelineProperty->Set(j_obj.dump().c_str());
        ASSERT_F(manager->Refresh());

        ASSERT_S(pipeline->Stop());
        ASSERT_F(pipeline->Start());
    }

    {
        TraceInfo("------ Test 6 ------");
        // Start with a valid pipeline, add an invalid pipeline, update invalid pipeline to valid pipeline
        CommandLineArgs args = CreateCommandLineArgs("exeName --pipelines [{\"id\":\"id1\",\"definition\":\"videotestsrc pattern=snow ! fakesink\"}]");
        ComPtr<IApp> app = App::CreateWithArgs(args.Count(), args.Values());

        ComPtr<IPipelineManager> manager;
        ASSERT_S(CreatePipelineManager(manager.AddressOf(), app));
        ASSERT_S(manager->Initialize());

        // set to an invalid pipeline
        ComPtr<IStringProperty> pipelineProperty;
        ASSERT_S(app->GetStringProperty(pipelineProperty.AddressOf(), "pipelines"));
        nlohmann::json j_obj = nlohmann::json::parse(pipelineProperty->Get());
        j_obj[1]["id"] = "id2";
        j_obj[1]["definition"] = "notaplugin ! fakesink";
        pipelineProperty->Set(j_obj.dump().c_str());
        ASSERT_F(manager->Refresh());

        ComPtr<IPipeline> pipeline;
        ASSERT_F(manager->GetPipelineById(pipeline.AddressOf(), "id2"));

        // update to a valid pipeline
        j_obj[1]["definition"] = "videotestsrc ! fakesink";
        pipelineProperty->Set(j_obj.dump().c_str());
        ASSERT_S(manager->Refresh());
        ASSERT_S(manager->GetPipelineById(pipeline.AddressOf(), "id2"));
    }

    {
        TraceInfo("------ Test 7 ------");
        // Start with an invalid pipeline, update to a valid pipeline
        CommandLineArgs args = CreateCommandLineArgs("exeName --pipelines [{\"id\":\"id1\",\"definition\":\"notaplugin ! fakesink\"}]");
        ComPtr<IApp> app = App::CreateWithArgs(args.Count(), args.Values());

        ComPtr<IPipelineManager> manager;
        ASSERT_S(CreatePipelineManager(manager.AddressOf(), app));
        ASSERT_F(manager->Initialize());

        ComPtr<IPipeline> pipeline;
        ASSERT_F(manager->GetPipelineById(pipeline.AddressOf(), "id1"));

        // update to a valid pipeline
        ComPtr<IStringProperty> pipelineProperty;
        ASSERT_S(app->GetStringProperty(pipelineProperty.AddressOf(), "pipelines"));
        nlohmann::json j_obj = nlohmann::json::parse(pipelineProperty->Get());
        j_obj[0]["id"] = "id1";
        j_obj[0]["definition"] = "videotestsrc ! fakesink";
        pipelineProperty->Set(j_obj.dump().c_str());

        ASSERT_S(manager->Refresh());
        ASSERT_S(manager->GetPipelineById(pipeline.AddressOf(), "id1"));
    }

     GStreamer::Shutdown();
}