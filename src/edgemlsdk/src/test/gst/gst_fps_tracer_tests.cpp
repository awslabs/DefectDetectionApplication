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
#include <Panorama/message_broker.h>
#include <misc.h>
#include <env_vars.h>
#include <TestUtils.h>

using namespace Panorama;

TEST(EdgeApp, Tracers)
{
    HRESULT hr = S_OK;

    ComPtr<IMessageBroker> broker;
    ASSERT_S(MessageBroker::Create(broker.AddressOf(), nullptr));
    ASSERT_S(broker->Initialize());

    ManualResetEvent metric_published;
    ASSERT_S(broker->Subscribe("analytics", [&](IPayload* payload)
    {
        metric_published.Set();
    }));

    SetEnvVar("GST_PLUGIN_PATH", BuildDirectory()+"/lib");
    SetEnvVar("GST_TRACERS", "fps");
    GStreamer::Initialize();

    ComPtr<IApp> app = App::Create();
    ComPtr<IPipeline> pipeline;
    ASSERT_S(CreatePipeline(pipeline.AddressOf(), "id", "videotestsrc ! fakesink", app));

    pipeline->Start();
    ASSERT_TRUE(metric_published.WaitFor(3000));
    pipeline->Stop();

    UnsetEnvVar("GST_PLUGIN_PATH");
    UnsetEnvVar("GST_TRACERS");
    GStreamer::Shutdown();
}