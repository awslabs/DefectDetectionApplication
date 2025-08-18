#include <gtest/gtest.h>

#include <Panorama/python.h>

#include <gst_application/edge_app.h>

#include <TestUtils.h>

using namespace Panorama;

TEST(EdgeApp, LoadCppPlugin)
{
    HRESULT hr = S_OK;

    {
        // non existent plugin
        PluginDescriptor descriptor;

        descriptor.Type = PluginType::Cpp;
        descriptor.Location = "./libdoes_not_exist.so";

        ComPtr<IAppPlugin> plugin;
        ASSERT_F(LoadAppPlugin(plugin.AddressOf(), descriptor));
    }

    {
        // file exists but doesn't export plugin factory method
        PluginDescriptor descriptor;

        descriptor.Type = PluginType::Cpp;
        descriptor.Location = "libpanorama.core.so";

        ComPtr<IAppPlugin> plugin;
        ASSERT_F(LoadAppPlugin(plugin.AddressOf(), descriptor));
    }

    {
        // happy path
        PluginDescriptor descriptor;

        descriptor.Type = PluginType::Cpp;
        descriptor.Location = "libtest_app_plugin.so";

        ComPtr<IAppPlugin> plugin;
        ASSERT_S(LoadAppPlugin(plugin.AddressOf(), descriptor));
        ASSERT_FALSE(strcmp("test_plugin", plugin->Id()));
    }
}

TEST(EdgeApp, LoadPythonPlugin)
{
    HRESULT hr = S_OK;

    AppendPythonPath(PythonLibraryDirectory().c_str());
    AppendPythonPath( (BuildDirectory()+"/lib").c_str() );

    {
        // plugin doesn't exit
        PluginDescriptor descriptor;

        descriptor.Type = PluginType::Python;
        descriptor.Location = "non-existent;TestAppPlugin";

        ComPtr<IAppPlugin> plugin;
        ASSERT_F(LoadAppPlugin(plugin.AddressOf(), descriptor));
    }

    {
        // malformed location string
        PluginDescriptor descriptor;

        descriptor.Type = PluginType::Python;
        descriptor.Location = "test_pluginTestAppPlugin";

        ComPtr<IAppPlugin> plugin;
        ASSERT_F(LoadAppPlugin(plugin.AddressOf(), descriptor));
    }

    {
        // malformed implementation
        PluginDescriptor descriptor;

        descriptor.Type = PluginType::Python;
        descriptor.Location = "test_bad_plugin;TestAppPlugin";

        ComPtr<IAppPlugin> plugin;
        ASSERT_F(LoadAppPlugin(plugin.AddressOf(), descriptor));
    }

    {
        // happy path
        PluginDescriptor descriptor;

        descriptor.Type = PluginType::Python;
        descriptor.Location = "test_plugin;TestAppPlugin";

        ComPtr<IAppPlugin> plugin;
        ASSERT_S(LoadAppPlugin(plugin.AddressOf(), descriptor));
        ASSERT_FALSE(strcmp("test_py_plugin", plugin->Id()));
    }
}