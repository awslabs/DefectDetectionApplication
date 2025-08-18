#include <map>
#include <thread>

#include <gst/gst.h>
#include <gst/gsttracer.h>

#include <nlohmann/json.hpp>

#include <Panorama/comptr.h>
#include <Panorama/trace.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/eventing.h>
#include <Panorama/aws.h>
#include <Panorama/message_broker.h>

#include <app/device_env.h>

using namespace Panorama;

class FPS : public UnknownImpl<IUnknownAlias>
{
public:
    static HRESULT Create(FPS** ppObj, int32_t accumulationTime)
    {
        HRESULT hr = S_OK;
        CREATE_COM(FPS, ptr);
        CHECKHR(ptr->Initialize(accumulationTime));
        *ppObj = ptr.Detach();
        return hr;
    }

    ~FPS()
    {
        COM_DTOR(FPS);

        _shutdown = true;
        _sampleEvt.Set();
        if(_sampleThread.joinable())
        {
            _sampleThread.join();
        }

        COM_DTOR_FIN(FPS);
    }

    void AddSample(const char* id)
    {
        std::lock_guard<std::mutex> lk(_mtx);
        if(_counter.find(id) == _counter.end())
        {
            _counter[id] = 0;
        }

        _counter[id]++;
    }

private:
    HRESULT Initialize(int32_t accumulationTime)
    {
        HRESULT hr = S_OK;

        // Get the credential provider or this platform
        ComPtr<ICredentialProvider> credential_provider;
        CHECKHR(CreatePlatformCredentialProvider(credential_provider.AddressOf()));

        // Create the event broker
        CHECKHR(MessageBroker::Create(_broker.AddressOf(), credential_provider));
        CHECKHR(_broker->Initialize());

        // Setup thread that computes the FPS
        _accumulationTime = accumulationTime;
        _scalar = 1000.0f / static_cast<float>(_accumulationTime);
        _shutdown = false;

        _sampleThread = std::thread([&]()
        {
            while(_shutdown == false)
            {
                _sampleEvt.WaitFor(_accumulationTime);
                if(_shutdown)
                {
                    break;
                }

                nlohmann::json jObj;
                {
                    std::lock_guard<std::mutex> lk(_mtx);
                    for(auto iter = _counter.begin(); iter != _counter.end(); iter++)
                    {
                        jObj[iter->first] = iter->second * _scalar;
                        iter->second = 0;
                    }
                }

                jObj["type"] = "fps";
                _broker->PublishAsync("analytics", jObj.dump().c_str());
            }
        });

        return hr;
    }

    std::mutex _mtx;
    std::map<std::string, int32_t> _counter;
    int32_t _accumulationTime;
    float _scalar;
    bool _shutdown;
    std::thread _sampleThread;
    AutoResetEvent _sampleEvt;

    ComPtr<IMessageBroker> _broker;
};

G_BEGIN_DECLS
typedef struct _GstFPS GstFPS;
typedef struct _GstFPSClass GstFPSClass;

GType gst_fpsy_tracer_get_type(void);
#define GST_TYPE_FPS_TRACER (gst_fps_tracer_get_type())
#define GST_FPS_TRACER_CAST(obj) ((GstFPS *)(obj))

struct _GstFPS {
    GstTracer parent;
    FPS* fps = nullptr;
};

struct _GstFPSClass {
    GstTracerClass parent_class;
};

G_DEFINE_TYPE(GstFPS, gst_fps_tracer, GST_TYPE_TRACER);
G_END_DECLS

static void pad_push_pre(GstTracer* self, guint64 ts, GstPad *pad, GstBuffer* buffer)
{
    GstFPS* fps_tracer = GST_FPS_TRACER_CAST(self);
    if(fps_tracer != nullptr)
    {
        gchar* fullname = g_strdup_printf ("%s_%s", GST_DEBUG_PAD_NAME (pad));
        fps_tracer->fps->AddSample(fullname);
        g_free(fullname);
    }
}

static void pad_push_list_pre(GstTracer* self, GstClockTime ts, GstPad* pad, GstBufferList* list)
{
}

static void pad_pull_range_pre(GstTracer* self, GstClockTime ts, GstPad* pad, guint64 offset, guint size)
{
}

static void
gst_fps_tracer_finalize (GObject * obj)
{
    GstFPS* fps_tracer = GST_FPS_TRACER_CAST(obj);
    if(fps_tracer->fps != nullptr)
    {
        fps_tracer->fps->Release();
    }

    G_OBJECT_CLASS (gst_fps_tracer_parent_class)->finalize (obj);
}

static void
gst_fps_tracer_class_init(GstFPSClass *klass) 
{
    GObjectClass *gobject_class = G_OBJECT_CLASS(klass);
    gobject_class->finalize = gst_fps_tracer_finalize;
}

static void
gst_fps_tracer_init(GstFPS *self) 
{
    GstTracer *tracer = GST_TRACER (self);
    HRESULT hr = FPS::Create(&(self->fps), 1000);
    TraceInfo("Initializing FPS tracer");
    if(FAILED(hr))
    {
        TraceError("Unable to create FPS tracer: %s", ErrorCodeToString(hr));
    }
    else
    {
        gst_tracing_register_hook (tracer, "pad-push-pre", G_CALLBACK (pad_push_pre));
        gst_tracing_register_hook (tracer, "pad-push-list-pre", G_CALLBACK (pad_push_list_pre));
        gst_tracing_register_hook (tracer, "pad-pull-range-pre", G_CALLBACK (pad_pull_range_pre));
    }
}

static gboolean plugin_init(GstPlugin *plugin) 
{
    gst_tracer_register(plugin, "fps", gst_fps_tracer_get_type());
    return TRUE;
}

#define PACKAGE "fps"
GST_PLUGIN_DEFINE(GST_VERSION_MAJOR, GST_VERSION_MINOR, fps,
                  "Tracer to compute framerate of pipeline and elements", plugin_init, "1.0", "LGPL", "fps",
                  "http://todo.com")