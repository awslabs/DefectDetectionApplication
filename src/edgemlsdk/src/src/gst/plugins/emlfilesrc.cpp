#include <mutex>

#include <gst/gst.h>
#include <gst/base/gstpushsrc.h>

#include <nlohmann/json.hpp>
#include <Panorama/app.h>
#include <Panorama/message_broker.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/eventing.h>
#include <Panorama/gst.h>
#include <Panorama/message_broker.h>
#include <misc.h>
#include <app/device_env.h>

using namespace Panorama;

class Trigger : public UnknownImpl<IUnknownAlias>
{
public:
    static HRESULT Create(Trigger** ppObj)
    {
        COM_FACTORY(Trigger, Initialize());
    }

    ~Trigger()
    {
        COM_DTOR(Trigger);
        if(_sub_token >= 0)
        {
            PEEKHR(_broker->Unsubscribe(_sub_token));
        }
        COM_DTOR_FIN(Trigger);
    }

    HRESULT SetSubscriptionId(const char* sub_id)
    {
        HRESULT hr = S_OK;
        CHECKNULL_OR_EMPTY(sub_id, E_INVALIDARG);

        if(_sub_token >= 0)
        {
            CHECKHR(_broker->Unsubscribe(_sub_token));
        }

        _sub_id = sub_id;
        CHECKHR(_broker->Subscribe(_sub_id.c_str(), [&](IPayload* message)
        {
            TriggerReceived(message);
        }));
        _sub_token = hr;

        return S_OK;
    }

    const char* GetSubscriptionId()
    {
        return _sub_id.c_str();
    }

    bool WaitForTrigger()
    {
        _trigger.Wait();
        return _shutting_down == false;
    }

    std::string FilePath()
    {
        return _file_path;
    }

    bool HasCorrelationId()
    {
        return _correlation_id.empty() == false;
    }

    std::string CorrelationId()
    {
        return _correlation_id;
    }

    void ElementState(GstStateChange transition)
    {
        // Todo: Currently this wouldn't support pausing then playing again
        if(transition == GST_STATE_CHANGE_PAUSED_TO_READY) 
        {
            _shutting_down = true;
            _trigger.Set();
        }
    }

private:
    HRESULT Initialize()
    {
        HRESULT hr = S_OK;

        ComPtr<ICredentialProvider> credential_provider;
        CHECKHR(CreatePlatformCredentialProvider(credential_provider.AddressOf()));
        CHECKHR(MessageBroker::Create(_broker.AddressOf(), credential_provider));
        CHECKHR(_broker->Initialize());
        _sub_token = -1;
        return S_OK;
    }

    void TriggerReceived(IPayload* message)
    {
        HRESULT hr = S_OK;
        std::string msg = message->SerializeAsString();

        if(nlohmann::json::accept(msg) == false)
        {
            TraceError("Message received is not valid JSON: %s", msg.c_str());
            return;
        }

        nlohmann::json jObj = nlohmann::json::parse(msg);
        CHECKIF_MSG(ValidateJsonProperty<const char*>(jObj, "file-path") == false,, "emlfilesrc: file-path was not specified");
        CHECKIF_MSG(ValidateJsonProperty<const char*>(jObj, "correlation-id") == false,, "emlfilesrc: correlation-id was provided, but is not a string");
        _file_path = jObj["file-path"];

        if(jObj.contains("correlation-id"))
        {
            _correlation_id = jObj["correlation-id"];
        }
        else
        {
            _correlation_id = "";
        }

        TraceInfo("Received trigger: File Path = %s and correlation_id = %s", _file_path.c_str(), _correlation_id.c_str());
        _trigger.Set();
    }

    ComPtr<IMessageBroker> _broker;
    std::string _sub_id;
    int32_t _sub_token = -1;
    AutoResetEvent _trigger;
    bool _shutting_down = false;
    std::string _file_path;
    std::string _correlation_id;
};

// ================ GStreamer Boiler Plate Below ======================
G_BEGIN_DECLS

#define GST_TYPE_EMLFILESRC   (gst_emlfilesrc_get_type())
#define GST_EMLFILESRC(obj)   (G_TYPE_CHECK_INSTANCE_CAST((obj),GST_TYPE_EMLFILESRC,GstEmlfilesrc))
#define GST_EMLFILESRC_CLASS(klass)   (G_TYPE_CHECK_CLASS_CAST((klass),GST_TYPE_EMLFILESRC,GstEmlfilesrcClass))
#define GST_IS_EMLFILESRC(obj)   (G_TYPE_CHECK_INSTANCE_TYPE((obj),GST_TYPE_EMLFILESRC))
#define GST_IS_EMLFILESRC_CLASS(obj)   (G_TYPE_CHECK_CLASS_TYPE((klass),GST_TYPE_EMLFILESRC))
#define PLUGIN_DESCRIPTION "Source plugin that can be triggered from the message broker to load a file"
#ifndef VERSION
#define VERSION "1.0.0"
#endif
#ifndef PACKAGE
#define PACKAGE "EdgeML-SDK"
#endif
#ifndef PACKAGE_NAME
#define PACKAGE_NAME "EdgeML-SDK Plugins"
#endif
#ifndef GST_PACKAGE_ORIGIN
#define GST_PACKAGE_ORIGIN "https://alpha.www.docs.aws.a2z.com/edgeml-sdk/v1/1.0/index.html"
#endif

typedef struct _GstEmlfilesrc GstEmlfilesrc;
typedef struct _GstEmlfilesrcClass GstEmlfilesrcClass;
struct _GstEmlfilesrc
{
    GstPushSrc base_emlfilesrc;
    Trigger* Trigger;
};

struct _GstEmlfilesrcClass
{
    GstPushSrcClass base_emlfilesrc_class;
};

static GstElementClass *parent_class = NULL;
GType gst_emlfilesrc_get_type(void);

G_END_DECLS

GST_DEBUG_CATEGORY_STATIC(gst_emlfilesrc_debug_category);
#define GST_CAT_DEFAULT gst_emlfilesrc_debug_category

gboolean emlfilesrc_init(GstPlugin* emlfilesrc)
{
    return gst_element_register(emlfilesrc, "emlfilesrc", GST_RANK_NONE, GST_TYPE_EMLFILESRC);
}

GST_PLUGIN_DEFINE(GST_VERSION_MAJOR,
    GST_VERSION_MINOR,
    emlfilesrc,
    PLUGIN_DESCRIPTION,
    emlfilesrc_init, VERSION, "Proprietary", PACKAGE_NAME, GST_PACKAGE_ORIGIN)

G_DEFINE_TYPE_WITH_CODE(GstEmlfilesrc, gst_emlfilesrc, GST_TYPE_PUSH_SRC,
    GST_DEBUG_CATEGORY_INIT(gst_emlfilesrc_debug_category, "emlfilesrc", 0,
        "debug category for emlfilesrc element"));

typedef enum
{
    PROP_0,
    SUBSCRIPTION_ID,
    FILEPATH
} PluginProperty;

static GstFlowReturn emlfilesrc_create(GstPushSrc *src, GstBuffer **buf) 
{
    HRESULT hr = S_OK;

    GstEmlfilesrc* emlfilesrc = GST_EMLFILESRC(src);
    CHECKIF_MSG(emlfilesrc->Trigger == nullptr, GST_FLOW_ERROR, "Trigger for emlfilesrc was not successfully created");

    // Wait for a trigger
    if(emlfilesrc->Trigger->WaitForTrigger() == false)
    {
        // Pipeline is shutting down
        *buf = gst_buffer_new();
        GST_BUFFER_PTS(*buf) = GST_CLOCK_TIME_NONE;
        GST_BUFFER_DTS(*buf) = GST_CLOCK_TIME_NONE;
        return GST_FLOW_EOS;
    }

    CHECKNULL_OR_EMPTY(emlfilesrc->Trigger->FilePath().c_str(), GST_FLOW_ERROR);

    // Read data from the file into a GstBuffer
    FILE* fptr = fopen(emlfilesrc->Trigger->FilePath().c_str(), "rb");
    CHECKNULL_MSG(fptr, GST_FLOW_ERROR, "Could not read from file %s", emlfilesrc->Trigger->FilePath().c_str());

    fseek(fptr, 0, SEEK_END);
    long len = ftell(fptr);
    if(len <= 0)
    {
        TraceError("ftell failed");
        fclose(fptr);
        return GST_FLOW_ERROR;
    }
    fseek(fptr, 0, SEEK_SET);

    GstBuffer* buffer;
    buffer = gst_buffer_new_allocate(NULL, len, NULL);
    if(buffer == nullptr)
    {
        TraceError("Could not allocate new gst buffer");
        fclose(fptr);
        return GST_FLOW_ERROR;
    }

    GstMapInfo map;
    gst_buffer_map(buffer, &map, GST_MAP_WRITE);
    size_t bytes_read = fread(map.data, sizeof(uint8_t), static_cast<size_t>(len), fptr);
    gst_buffer_unmap(buffer, &map);

    fclose(fptr);
    CHECKIF(bytes_read != len, GST_FLOW_ERROR);

    if(emlfilesrc->Trigger->HasCorrelationId())
    {
        CHECK_FAIL(SetBufferCorrelationId(buffer, emlfilesrc->Trigger->CorrelationId().c_str()), GST_FLOW_ERROR);
    }

    GST_BUFFER_PTS(buffer) = 0;
    GST_BUFFER_DTS(buffer) = 0;

    *buf = buffer;
    return GST_FLOW_OK;
}

void gst_emlfilesrc_set_property(GObject* object, guint property_id, const GValue* value, GParamSpec* pspec)
{
    GstEmlfilesrc* emlfilesrc = GST_EMLFILESRC(object);
    switch (property_id) 
    {
        case PluginProperty::SUBSCRIPTION_ID:
            if(emlfilesrc->Trigger != nullptr)
            {
                emlfilesrc->Trigger->SetSubscriptionId(g_value_get_string(value));
            }

            break;

        default:
            G_OBJECT_WARN_INVALID_PROPERTY_ID(object, property_id, pspec);
            break;
    }
}

void gst_emlfilesrc_get_property(GObject* object, guint property_id, GValue* value, GParamSpec* pspec)
{
    GstEmlfilesrc* emlfilesrc = GST_EMLFILESRC(object);
    switch (property_id) 
    {
        case PluginProperty::SUBSCRIPTION_ID:
            if(emlfilesrc->Trigger != nullptr)
            {
                g_value_set_string(value, emlfilesrc->Trigger->GetSubscriptionId());
            }

            break;

        default:
            G_OBJECT_WARN_INVALID_PROPERTY_ID(object, property_id, pspec);
            break;
    }
}

static GstStateChangeReturn gst_emlfilesrc_change_state(GstElement *element, GstStateChange transition) 
{
    GstEmlfilesrc* emlfilesrc = GST_EMLFILESRC(element);
    emlfilesrc->Trigger->ElementState(transition);
    return GST_ELEMENT_CLASS(parent_class)->change_state(element, transition);
}

static GstStaticPadTemplate gst_emlfilesrc_src_template =
GST_STATIC_PAD_TEMPLATE("src",
    GST_PAD_SRC,
    GST_PAD_ALWAYS,
    GST_STATIC_CAPS("ANY")
);

/* class initialization */
static gboolean emlfilesrc_start(GstBaseSrc *src) 
{
    return true;
}

void gst_emlfilesrc_dispose(GObject* object)
{
    GstEmlfilesrc* emlfilesrc = GST_EMLFILESRC(object);
    GST_DEBUG_OBJECT(emlfilesrc, "dispose");

    /* clean up as possible.  may be called multiple times */
    G_OBJECT_CLASS(gst_emlfilesrc_parent_class)->dispose(object);
}

void gst_emlfilesrc_finalize(GObject* object)
{
    GstEmlfilesrc* emlfilesrc = GST_EMLFILESRC(object);

    if(emlfilesrc->Trigger != nullptr)
    {
        delete emlfilesrc->Trigger;
        emlfilesrc->Trigger = nullptr;
    }

    GST_DEBUG_OBJECT(emlfilesrc, "finalize");
    G_OBJECT_CLASS(gst_emlfilesrc_parent_class)->finalize(object);
}

void gst_emlfilesrc_class_init(GstEmlfilesrcClass* klass)
{
    GObjectClass* gobject_class = (GObjectClass*)klass;
    GstElementClass* gstelement_class = (GstElementClass*)klass;
    GstBaseSrcClass *base_src_class = GST_BASE_SRC_CLASS(klass);
    GstPushSrcClass *push_src_class = GST_PUSH_SRC_CLASS(klass);

    // Set klass details
    gst_element_class_set_details_simple(gstelement_class,
        "EdgeML-SDK Triggerable File Source",
        "Source/Video",
        PLUGIN_DESCRIPTION, "aws");

    // parent class
    gstelement_class->change_state = gst_emlfilesrc_change_state;
    parent_class = static_cast<GstElementClass*>(g_type_class_peek_parent(klass));

    // Add static pads
    gst_element_class_add_static_pad_template(gstelement_class, &gst_emlfilesrc_src_template);

    // Buffer Creation Methods
    base_src_class->start = emlfilesrc_start;
    push_src_class->create = emlfilesrc_create;

    // Finalize
    gobject_class->finalize = gst_emlfilesrc_finalize;

    /* Properties */
    gobject_class->set_property = gst_emlfilesrc_set_property;
    gobject_class->get_property = gst_emlfilesrc_get_property;

    g_object_class_install_property(gobject_class, PluginProperty::SUBSCRIPTION_ID,
        g_param_spec_string("subscription-id", "Subscription Id", "Subscription id to listen to.",
                         "", static_cast<GParamFlags>(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));
}

void gst_emlfilesrc_init(GstEmlfilesrc* emlfilesrc)
{
    HRESULT hr = S_OK;
    CHECK_FAIL(Trigger::Create(&emlfilesrc->Trigger), );
}