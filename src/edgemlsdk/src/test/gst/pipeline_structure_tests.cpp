#include <gtest/gtest.h>

#include <Panorama/comptr.h>
#include <Panorama/app.h>
#include <Panorama/gst.h>
#include <gst/gstrunner/gst_runner.h>
#include <TestUtils.h>

using namespace Panorama;

TEST(GstTests, PipelineStructureTest)
{
    HRESULT hr = S_OK;
    int argc;
    char** argv;

    GStreamer::Initialize(2);

    {
        CommandLineArgs args = CreateCommandLineArgs("exeName --PATTERN {\"type\":\"string\", \"value\":\"ball\"} --GAMMA_DECODE {\"type\":\"string\", \"value\":\"true\"}");
        ComPtr<IApp> app = App::CreateWithArgs(args.Count(), args.Values());
        ASSERT_TRUE(app);

        std::string pipeline = "videotestsrc name=src pattern=${PATTERN} ! videoconvert ! videoscale name=scale gamma-decode=${GAMMA_DECODE} ! progressreport ! fakesink";
        PipelineStructure structure;
        ASSERT_S(GetPipelineStructure(&structure, pipeline.c_str(), app));

        std::string expansion;
        ExpandPipeline(&expansion, &structure, pipeline.c_str());
        ASSERT_EQ(expansion.compare("videotestsrc name=src pattern=ball ! videoconvert ! videoscale name=scale gamma-decode=true ! progressreport ! fakesink"), 0);

        ASSERT_NE(nullptr, structure.Pipeline);
        ASSERT_EQ(structure.DynamicProperties.size(), 2);
        ASSERT_EQ(structure.DynamicProperties[0].PropertyName.compare("pattern"), 0);
        ASSERT_EQ(structure.DynamicProperties[1].PropertyName.compare("gamma-decode"), 0);
    }

    {
        CommandLineArgs args = CreateCommandLineArgs("exeName --TEXT {\"type\":\"string\", \"value\":\"hello world\"}");
        ComPtr<IApp> app = App::CreateWithArgs(args.Count(), args.Values());
        ASSERT_TRUE(app);

        // Space in value of {TEXT}
        std::string pipeline = "videotestsrc ! videoconvert ! textoverlay name=overlay text=\"${TEXT}\" font-desc=Sans,64 ! fakesink";
        PipelineStructure structure;
        ASSERT_S(GetPipelineStructure(&structure, pipeline.c_str(), app));
    }

    {
        CommandLineArgs args = CreateCommandLineArgs("exeName --TEXT {\"type\":\"string\", \"value\":\"hello world\"}");
        ComPtr<IApp> app = App::CreateWithArgs(args.Count(), args.Values());
        ASSERT_TRUE(app);

        // Invalid pipeline
        std::string pipeline = "notaplugin ! textoverlay name=overlay text=\"${TEXT}\" font-desc=Sans,64 ! fakesink";
        PipelineStructure structure;
        ASSERT_F(GetPipelineStructure(&structure, pipeline.c_str(), app));
    }

    {
        CommandLineArgs args = CreateCommandLineArgs("exeName --TEXT {\"type\":\"string\", \"value\":\"\\\"hell\\\\o world\"}");
        ComPtr<IApp> app = App::CreateWithArgs(args.Count(), args.Values());
        ASSERT_TRUE(app);

        // Special characters in value of {TEXT}
        std::string pipeline = "videotestsrc ! videoconvert ! textoverlay name=overlay text=\"${TEXT}\" font-desc=Sans,64 ! fakesink";
        PipelineStructure structure;
        ASSERT_S(GetPipelineStructure(&structure, pipeline.c_str(), app));

        std::string expansion;
        ExpandPipeline(&expansion, &structure, pipeline.c_str());
        ASSERT_EQ(expansion.compare("videotestsrc ! videoconvert ! textoverlay name=overlay text=\"\\\"hell\\\\o world\" font-desc=Sans,64 ! fakesink"), 0);
    }

    {
        ComPtr<IApp> app = App::Create();
        ASSERT_TRUE(app);

        // Handling caps property that has multiple equals
        std::string pipeline = "videotestsrc ! capsfilter caps=video/x-raw,format=GRAY8 ! videoconvert ! fakesink";
        PipelineStructure structure;
        ASSERT_S(GetPipelineStructure(&structure, pipeline.c_str(), app));
    }
}