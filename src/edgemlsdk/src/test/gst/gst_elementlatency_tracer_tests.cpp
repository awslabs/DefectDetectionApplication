#include <thread>
#include <functional>

#include <gtest/gtest.h>
#include <nlohmann/json.hpp>

#include <Panorama/app.h>
#include <Panorama/aws.h>
#include <Panorama/comptr.h>
#include <Panorama/eventing.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/gst.h>
#include <Panorama/message_broker.h>
#include <Panorama/properties.h>

#include <misc.h>
#include <env_vars.h>
#include <TestUtils.h>

using namespace Panorama;

// MQTT scheme for message broker
std::string mqttSchema = 
"{                                                                                  "
"    \"targets\": [                                                                 "
"        {                                                                          "
"            \"protocol\": \"mqtt\",                                                "
"            \"name\": \"mqtt_1\",                                                  "
"            \"mqtt_options\": {                                                    "
"                \"endpoint\": \"a5h6960s3xow6-ats.iot.us-west-2.amazonaws.com\",   "
"                \"region\": \"us-west-2\"                                          "
"            }                                                                      "
"        }                                                                          "
"    ],                                                                             "
"    \"pipes\": [                                                                   "
"        {                                                                          "
"            \"message_id\": \"analytics\",                                         "
"            \"destinations\": [                                                    "
"                {                                                                  "
"                    \"target_name\": \"mqtt_1\",                                   "
"                    \"mqtt_message_options\": {                                    "
"                        \"topic\": \"8f7a2e1b9c4d6f0a\"                            "
"                    }                                                              "
"                }                                                                  "
"            ]                                                                      "
"        }                                                                          "
"    ]                                                                              "
"}                                                                                  ";

void checkPayloadHeader(const char * msg)
{
    if (!msg)
    {
        FAIL() << "Message from message broker was nullptr";
    }
    nlohmann::json j = nlohmann::json::parse(msg);
    ASSERT_TRUE(j.contains("type"));
    ASSERT_EQ(j["type"], "elementlatency");
}

void testPipeline(ComPtr<IMessageBroker> broker, std::string pipelineDefinition, int runTimeSeconds, MessageReceivedCallback cb)
{
    HRESULT hr = S_OK;

    ComPtr<IApp> app = App::Create();
    ComPtr<IPipeline> pipeline;

    ASSERT_S(broker->Subscribe("analytics", std::move(cb)));
    int32_t subscriptionCancellationToken = hr;

    ASSERT_S(CreatePipeline(pipeline.AddressOf(), "id", pipelineDefinition.c_str(), app));

    pipeline->Start();
    sleep(runTimeSeconds);
    pipeline->Stop();

    ASSERT_S(broker->Unsubscribe(subscriptionCancellationToken));
}

TEST(GstTracerTest, ElementLatencyTracer)
{
    HRESULT hr = S_OK;

    // ratio of allowed difference from expected
    double buffer = 0.3;

    // Get the credential provider for AWS credentials set in your environment variables
    ComPtr<ICredentialProvider> credentials = Panorama_Aws::DefaultCredentialProvider();
    ASSERT_TRUE(credentials != nullptr);

    ComPtr<IMessageBroker> broker;
    MessageBroker::SetDefaultConfig(mqttSchema.c_str());
    ASSERT_S(MessageBroker::Create(broker.AddressOf(), credentials));
    ASSERT_S(broker->Initialize());

    SetEnvVar("GST_PLUGIN_PATH", BuildDirectory()+"/lib");
    SetEnvVar("GST_TRACERS", "elementlatency");
    GStreamer::Initialize();

    /**
     * Test "videotestsrc ! fakesink"
    */
    ManualResetEvent metric_published;
    testPipeline(broker, "videotestsrc ! fakesink", 2, [&](IPayload* payload)
    {
        const char * msg = payload->SerializeAsString();
        checkPayloadHeader(msg);
        metric_published.Set();

        nlohmann::json j = nlohmann::json::parse(msg);
        // type header
        ASSERT_EQ(j.size(), 1);
    });
    // check callback actually ran (received data)
    ASSERT_TRUE(metric_published.WaitFor(0));
    metric_published.Reset();

    /**
     * Test "videotestsrc ! identity sleep-time=100000 ! fakesink"
     * identity sleep-time is in microseconds (sleep 0.1 seconds)
    */
    testPipeline(broker, "videotestsrc ! identity sleep-time=100000 ! fakesink", 2, [&](IPayload* payload)
    {
        const char * msg = payload->SerializeAsString();
        checkPayloadHeader(msg);
        metric_published.Set();

        nlohmann::json j = nlohmann::json::parse(msg);
        // type header, identity0 entry
        ASSERT_EQ(j.size(), 2);

        ASSERT_TRUE(j.contains("identity0"));
        ASSERT_TRUE(j["identity0"].contains("count"));
        ASSERT_TRUE(j["identity0"].contains("mean"));
        ASSERT_TRUE(j["identity0"].contains("variance"));

        // Tracer reports every 1 second. 1 second / 0.1 second per sleep = 10 samples per report
        // Count may vary, so give some buffer
        ASSERT_TRUE(10.0 * (1.0 - buffer) <= j["identity0"]["count"] && 10.0 * (1.0 + buffer) >= j["identity0"]["count"]);

        // Mean may vary, so give some buffer
        // Mean is reported in nanoseconds
        ASSERT_TRUE(100000000.0 * (1.0 - buffer) <= j["identity0"]["mean"] && 100000000.0 * (1.0 + buffer) >= j["identity0"]["mean"]);

        // Variance is all over the place, don't check
    });
    // check callback actually ran (received data)
    ASSERT_TRUE(metric_published.WaitFor(0));
    metric_published.Reset();

    /**
     * Test "videotestsrc ! identity sleep-time=20000 ! identity sleep-time=80000 ! fakesink"
     * identity sleep-time is in microseconds (sleep 0.02 then 0.08 seconds)
    */
    testPipeline(broker, "videotestsrc ! identity sleep-time=20000 ! identity sleep-time=80000 ! fakesink", 2, [&](IPayload* payload)
    {
        const char * msg = payload->SerializeAsString();
        checkPayloadHeader(msg);
        metric_published.Set();

        nlohmann::json j = nlohmann::json::parse(msg);
        
        // gstreamer seems to remember the old created pipeline elements for naming purposes
        // type header, identity1, identity2 entries
        ASSERT_EQ(j.size(), 3);
        ASSERT_TRUE(j.contains("identity1"));
        ASSERT_TRUE(j["identity1"].contains("count"));
        ASSERT_TRUE(j["identity1"].contains("mean"));
        ASSERT_TRUE(j["identity1"].contains("variance"));

        ASSERT_TRUE(j.contains("identity2"));
        ASSERT_TRUE(j["identity2"].contains("count"));
        ASSERT_TRUE(j["identity2"].contains("mean"));
        ASSERT_TRUE(j["identity2"].contains("variance"));

        // Tracer reports every 1 second. 1 second / (0.02 second per sleep + 0.08 second per sleep) = 10 samples per element
        // Count may vary, so give some buffer
        ASSERT_TRUE(10.0 * (1.0 - buffer) <= j["identity1"]["count"] && 10.0 * (1.0 + buffer) >= j["identity1"]["count"]);
        ASSERT_TRUE(10.0 * (1.0 - buffer) <= j["identity2"]["count"] && 10.0 * (1.0 + buffer) >= j["identity2"]["count"]);

        // Mean may vary, so give some buffer
        // Mean is reported in nanoseconds
        ASSERT_TRUE(20000000.0 * (1.0 - buffer) <= j["identity1"]["mean"] && 20000000.0 * (1.0 + buffer) >= j["identity1"]["mean"]);
        ASSERT_TRUE(80000000.0 * (1.0 - buffer) <= j["identity2"]["mean"] && 80000000.0 * (1.0 + buffer) >= j["identity2"]["mean"]);

        // Variance is all over the place, don't check
    });
    // check callback actually ran (received data)
    ASSERT_TRUE(metric_published.WaitFor(0));
    metric_published.Reset();

    /**
     * Test "videotestsrc ! identity sleep-time=20000 ! identity sleep-time=30000 ! identity sleep-time=50000 ! fakesink"
     * identity sleep-time is in microseconds (sleep 0.02, 0.03, 0.05 seconds)
    */
    testPipeline(broker, "videotestsrc ! identity sleep-time=20000 ! identity sleep-time=30000 ! identity sleep-time=50000 ! fakesink", 2, [&](IPayload* payload)
    {
        const char * msg = payload->SerializeAsString();
        checkPayloadHeader(msg);
        metric_published.Set();

        nlohmann::json j = nlohmann::json::parse(msg);
        
        // gstreamer seems to remember the old created pipeline elements for naming purposes
        // type header, identity1, identity2 entries
        ASSERT_EQ(j.size(), 4);
        ASSERT_TRUE(j.contains("identity3"));
        ASSERT_TRUE(j["identity3"].contains("count"));
        ASSERT_TRUE(j["identity3"].contains("mean"));
        ASSERT_TRUE(j["identity3"].contains("variance"));

        ASSERT_TRUE(j.contains("identity4"));
        ASSERT_TRUE(j["identity4"].contains("count"));
        ASSERT_TRUE(j["identity4"].contains("mean"));
        ASSERT_TRUE(j["identity4"].contains("variance"));

        ASSERT_TRUE(j.contains("identity5"));
        ASSERT_TRUE(j["identity5"].contains("count"));
        ASSERT_TRUE(j["identity5"].contains("mean"));
        ASSERT_TRUE(j["identity5"].contains("variance"));

        // Tracer reports every 1 second. 1 second / (0.02 second per sleep + 0.08 second per sleep) = 10 samples per element
        // Count may vary, so give some buffer
        ASSERT_TRUE(10.0 * (1.0 - buffer) <= j["identity3"]["count"] && 10.0 * (1.0 + buffer) >= j["identity3"]["count"]);
        ASSERT_TRUE(10.0 * (1.0 - buffer) <= j["identity4"]["count"] && 10.0 * (1.0 + buffer) >= j["identity4"]["count"]);
        ASSERT_TRUE(10.0 * (1.0 - buffer) <= j["identity5"]["count"] && 10.0 * (1.0 + buffer) >= j["identity5"]["count"]);

        // Mean may vary, so give some buffer
        // Mean is reported in nanoseconds
        ASSERT_TRUE(20000000.0 * (1.0 - buffer) <= j["identity3"]["mean"] && 20000000.0 * (1.0 + buffer) >= j["identity3"]["mean"]);
        ASSERT_TRUE(30000000.0 * (1.0 - buffer) <= j["identity4"]["mean"] && 80000000.0 * (1.0 + buffer) >= j["identity4"]["mean"]);
        ASSERT_TRUE(50000000.0 * (1.0 - buffer) <= j["identity5"]["mean"] && 80000000.0 * (1.0 + buffer) >= j["identity5"]["mean"]);

        // Variance is all over the place, don't check
    });
    // check callback actually ran (received data)
    ASSERT_TRUE(metric_published.WaitFor(0));
    metric_published.Reset();

    GStreamer::Shutdown();
    UnsetEnvVar("GST_PLUGIN_PATH");
    UnsetEnvVar("GST_TRACERS");
}
