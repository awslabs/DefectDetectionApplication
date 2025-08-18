#include <mutex>
#include <map>
#include <sstream>

#include <Panorama/comptr.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/gst.h>
#include <env_vars.h>

using namespace Panorama;

#define PANORAMA_PLUGIN_PATH "/usr/lib/panoramagst"
#define GST_PLUGIN_PATH "GST_PLUGIN_PATH"

class SmartGstElement : public UnknownImpl<IGstElement>
{
public:
    static HRESULT Create(IGstElement** ppObj, GstElement* element)
    {
        CHECKNULL(ppObj, E_POINTER);
        *ppObj = nullptr;

        CHECKNULL(element, E_INVALIDARG);
        ComPtr<SmartGstElement> ptr;
        ptr.Attach(new (std::nothrow) SmartGstElement());
        CHECKNULL(ptr, E_OUTOFMEMORY);

        ptr->_element = element;
        *ppObj = ptr.Detach();
        return S_OK;
    }

    ~SmartGstElement()
    {
        if (_element != nullptr)
        {
            gst_object_unref(_element);
        }
    }

    GstElement* Element() override
    {
        return _element;
    }

private:
    SmartGstElement() = default;
    GstElement* _element = nullptr;
};

DLLAPI HRESULT GstElementMakeCom(IGstElement** ppObj, GstElement* element)
{
    return SmartGstElement::Create(ppObj, element);
}

DLLAPI TraceLevel GstLevelToTraceLevel(GstDebugLevel level)
{
    switch (level)
    {
    case GST_LEVEL_ERROR:
        return TraceLevel::Error;
        break;
    case GST_LEVEL_WARNING:
        return TraceLevel::Warning;
        break;
    case GST_LEVEL_FIXME:
        return TraceLevel::Warning;
        break;
    case GST_LEVEL_INFO:
        return TraceLevel::Information;
        break;
    case GST_LEVEL_DEBUG:
        return TraceLevel::Verbose;
        break;
    default:
        return TraceLevel::Verbose;
        break;
    }
}

DLLAPI GstDebugLevel TraceLevelToGstLevel(TraceLevel level)
{
    switch (level)
    {
    case TraceLevel::Error:
        return GST_LEVEL_ERROR;

    case TraceLevel::Warning:
        return GST_LEVEL_WARNING;

    // gst logging is too verbose, map to a level lower for info/verbose
    case TraceLevel::Information:
        return GST_LEVEL_FIXME;

    case TraceLevel::Verbose:
        return GST_LEVEL_INFO;

    default:
        return GST_LEVEL_DEBUG;
    }
}

static void LogFunction(GstDebugCategory* category, GstDebugLevel level, const gchar* file, const gchar* function, gint line, GObject* object, GstDebugMessage* message, gpointer user_data)
{
    Trace(GstLevelToTraceLevel(level), NowAsTimestamp(), line, file, gst_debug_message_get(message));
}

DLLAPI HRESULT InitializeGStreamerWithArgs(int argc, char* argv[], int gstLogLevel)
{
    static std::mutex initMtx;
    static bool initialized = false;

    std::lock_guard<std::mutex> lk(initMtx);
    if (initialized == false)
    {
        if(gst_is_initialized() == false)
        {
            // Add the default plugin path for EdgeML-SDK
            std::string plugin_path = GetEnvVar("GST_PLUGIN_PATH");
            if(plugin_path.empty() == false)
            {
                plugin_path = plugin_path + ":" + PANORAMA_PLUGIN_PATH;
            }
            else
            {
                plugin_path = PANORAMA_PLUGIN_PATH;
            }

            TraceInfo("Setting GST_PLUGIN_PATH=%s", plugin_path.c_str());
            SetEnvVar("GST_PLUGIN_PATH", plugin_path.c_str());

            TraceInfo("Initialize gstreamer");
            gst_init(&argc, &argv);
        }

        TraceInfo("Setting gst log level to %d", gstLogLevel);
        
        gst_debug_add_log_function(LogFunction, nullptr, nullptr);
        gst_debug_remove_log_function(gst_debug_log_default);
        gst_debug_set_default_threshold(static_cast<GstDebugLevel>(gstLogLevel));

        initialized = true;
    }

    return S_OK;
}

DLLAPI HRESULT InitializeGStreamer(int gstLogLevel)
{
    // Initialize GStreamer so that the logs are routed to V2 SDK Trace
    return InitializeGStreamerWithArgs(0, nullptr, static_cast<int>(gstLogLevel));
}

DLLAPI HRESULT ShutdownGStreamer()
{
    gst_debug_remove_log_function(LogFunction);
    gst_deinit();
    return S_OK;
}
