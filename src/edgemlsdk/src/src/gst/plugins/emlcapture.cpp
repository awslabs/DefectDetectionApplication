#include <mutex>

#include <gst/gst.h>
#include <gst/base/gstbasetransform.h>

#include <nlohmann/json.hpp>

#include <Panorama/message_broker.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/eventing.h>
#include <Panorama/gst.h>
#include <app/device_env.h>
#include <misc.h>
#include <scheduling.h>
#include <queue>

using namespace Panorama;

struct CaptureContext
{
    std::string BufferMessageId;
    bool Async = true;
    int32_t Interval = 0;
};

struct MetaCaptureContext
{
    std::string Impl;
    std::string MessageId;
};

class GstDataCapture : public UnknownImpl<IUnknownAlias>
{
public:
    static HRESULT Create(GstDataCapture** ppObj, GstElement* parent)
    {
        COM_FACTORY(GstDataCapture, Initialize(parent));
    }

    ~GstDataCapture()
    {
        COM_DTOR(GstDataCapture);
        if(_subscription_token >= 0)
        {
            PEEKHR(_broker->Unsubscribe(_subscription_token));
        }
        COM_DTOR_FIN(GstDataCapture);
    }

    HRESULT NewBuffer(GstBuffer* buf)
    {
        HRESULT hr = S_OK;

        if(_ctxt.Interval > 0)
        {
            Timestamp duration = NowAsTimestamp() - _interval_epoch;
            if(TimestampToMilliseconds(duration) >= _ctxt.Interval)
            {
                _capture.Set();
                _interval_epoch = NowAsTimestamp();
            }
        }

        if(_capture.WaitFor(0))
        {
            SendData(buf, _ctxt);
        }

        return hr;
    }

    void SetSubscriptionId(const char* subscription_id)
    {
        std::lock_guard<std::recursive_mutex> lk(_mtx);

        // unsubscribe from existing subscription
        if(SUCCEEDED(_subscription_token))
        {
            TraceVerbose("Unsubscribing from %s", _subscription_id.c_str());
            PEEKHR(_broker->Unsubscribe(_subscription_token));
        }

        // subscribe if subscription id is not null or empty
        _subscription_id = subscription_id == nullptr ? "" : subscription_id;
        if(_subscription_id.empty())
        {
            return;
        }

        // Subscribe
        TraceVerbose("Subscribing to %s", subscription_id);
        _subscription_token = _broker->Subscribe(subscription_id, [&](IPayload* payload)
        {
            TraceVerbose("Received capture request");

            // std::string request = payload->SerializeAsString();
            // CHECKIF_MSG(nlohmann::json::accept(request) == false, ,"Request is not valid JSON");
            // nlohmann::json jObj = nlohmann::json::parse(request);

            // CHECKIF_MSG(ValidateJsonProperty<const char*>(jObj, "buffer-message-id", false) == false, ,"Property 'buffer-message-id' was not a string");

            // // Get the buffer message id
            // std::string buffer_message_id = "";
            // if(jObj.contains("buffer-message-id"))
            // {
            //     std::string id = jObj["buffer-message-id"];
            //     SetBufferMessageId(id.c_str());
            // }

            // Signal to capture the next frame received
            _capture.Set();
        });

        PEEKHR(_subscription_token);
    }

    const char* GetSubscriptionId()
    {
        std::lock_guard<std::recursive_mutex> lk(_mtx);
        return _subscription_id.c_str();
    }

    void SetBufferMessageId(const char* id)
    {
        std::lock_guard<std::recursive_mutex> lk(_mtx);
        if (id != nullptr)
        {
            TraceVerbose("Setting buffer message id to %s", id);
            _ctxt.BufferMessageId = id;
        }
    }

    const char* GetBufferMessageId()
    {
        std::lock_guard<std::recursive_mutex> lk(_mtx);
        return _ctxt.BufferMessageId.c_str();
    }

    void SetBufferPropertiesId(const char* id)
    {
        std::lock_guard<std::recursive_mutex> lk(_mtx);
        if (id != nullptr)
        {
            TraceVerbose("Setting buffer properties id to %s", id);
            _buffer_properties_id = id;
        }
    }

    const char* GetBufferPropertiesId()
    {
        std::lock_guard<std::recursive_mutex> lk(_mtx);
        return _buffer_properties_id.c_str();
    }

    void SetInterval(int32_t interval)
    {
        std::lock_guard<std::recursive_mutex> lk(_mtx);
        TraceVerbose("Setting interval to %d", interval);
        _ctxt.Interval = interval;

        // check if continuous (Interval == 0)
        _ctxt.Interval == 0 ?   _capture.Set() :
                                _capture.Reset();

        if(_ctxt.Interval > 0)
        {
            SetIntervalEpoch();
        }
        else
        {
            _interval_epoch = -1;
        }
    }

    int32_t GetInterval()
    {
        std::lock_guard<std::recursive_mutex> lk(_mtx);
        return _ctxt.Interval;
    }

    void SetIntervalEpoch()
    {
        std::lock_guard<std::recursive_mutex> lk(_mtx);
        TraceVerbose("Setting interval epoch to now");
        _interval_epoch = NowAsTimestamp();
    }

    void SetAsync(bool async)
    {
        std::lock_guard<std::recursive_mutex> lk(_mtx);
        TraceVerbose("Setting async to %s", async ? "true" : "false");
        _ctxt.Async = async;
    }

    bool GetAsync()
    {
        std::lock_guard<std::recursive_mutex> lk(_mtx);
        return _ctxt.Async;
    }

    HRESULT SetMeta(const char* meta)
    {
        std::lock_guard<std::recursive_mutex> lk(_mtx);
        CHECKNULL_OR_EMPTY(meta, E_INVALIDARG);
        _meta_ctxt.clear();
        std::vector<std::string> meta_to_capture = SplitString(meta, ',');
        for(auto iter = meta_to_capture.begin(); iter != meta_to_capture.end(); iter++)
        {
            std::vector<std::string> ctxt = SplitString(*iter, ':');
            CHECKIF_MSG(ctxt.size() != 2, E_INVALIDARG, "Meta string is not in appropriate format");
            _meta_ctxt.push_back({ctxt[0], ctxt[1]});
        }

        _meta = meta;
        return S_OK;
    }

    const char* GetMeta()
    {
        std::lock_guard<std::recursive_mutex> lk(_mtx);
        return _meta.c_str();
    }

private:
    GstDataCapture() = default;

    HRESULT Initialize(GstElement* parent)
    {
        HRESULT hr = S_OK;
        TraceVerbose("Initializing emlcapture plugin");
        CHECKNULL(parent, E_INVALIDARG);

        _parent = parent;
        _interval_epoch = -1;

        // Set defaults
        _ctxt.Interval = -1;
        _ctxt.BufferMessageId = "";
        _ctxt.Async = true;
        _buffer_properties_id = "";

        // Get the credential provider for this platform
        ComPtr<ICredentialProvider> credential_provider;
        CHECKHR(CreatePlatformCredentialProvider(credential_provider.AddressOf()));

        // Create the message broker
        CHECKHR(MessageBroker::Create(_broker.AddressOf(), credential_provider));
        CHECKHR(_broker->Initialize());

        return hr;
    }

    HRESULT SendData(GstBuffer* buffer, const CaptureContext& ctxt)
    {
        std::lock_guard<std::recursive_mutex> lk(_mtx);
        HRESULT hr = S_OK;

        GstMapInfo map;

        // Grab the correlation id for this buffer if it exists
        std::string id = "";
        ComPtr<IBuffer> correlation_id;
        if(SUCCEEDED(GetBufferCorrelationId(correlation_id.AddressOf(), buffer)))
        {
            id = correlation_id->AsString();
        }

        Timestamp now = NowAsTimestamp();

        // Publish the frame buffer
        if(ctxt.BufferMessageId.empty() == false)
        {
            TraceVerbose("Capturing buffer");

            // publish the GstBuffer to BufferMessageId
            CHECKIF(gst_buffer_map(buffer, &map, GST_MAP_READ) == false, E_FAIL);
            ComPtr<IBuffer> payloadBuffer;
            CHECKHR(CreateBuffer(payloadBuffer.AddressOf(), map.size));
            CHECKNULL(memcpy(payloadBuffer->Data(), map.data, map.size), E_FAIL);

            ComPtr<IPayload> payload;
            CHECKHR(MessageBroker::CreatePayload(payload.AddressOf(), payloadBuffer));
            CHECKHR(payload->SetCorrelationId(id.c_str()));
            CHECKHR(payload->SetTimestamp(now));
            CHECKHR(ctxt.Async ?
                _broker->PublishAsync(ctxt.BufferMessageId.c_str(), payload, nullptr) :
                _broker->Publish(ctxt.BufferMessageId.c_str(), payload));

            gst_buffer_unmap(buffer, &map);
        }

        // Publish any metadata
        for(auto iter = _meta_ctxt.begin(); iter != _meta_ctxt.end(); iter++)
        {
            TraceVerbose("Capturing metadata");
            ComPtr<IPayload> payload;
            HRESULT temp_hr = GStreamer::GetPayloadFromBuffer(payload.AddressOf(), buffer, iter->Impl.c_str());
            
            // Don't return on not found error, print warning and continue
            if(temp_hr == E_NOT_FOUND)
            {
                TraceWarning("Could not find '%s' implementation of PayloadMeta", iter->Impl.c_str());
                continue;
            }

            CHECKHR(temp_hr);
            CHECKHR(ctxt.Async ?
                _broker->PublishAsync(iter->MessageId.c_str(), payload) :
                _broker->Publish(iter->MessageId.c_str(), payload));
        }
        
        // Publish the properties
        if(_buffer_properties_id.empty() == false)
        {
            TraceVerbose("Capturing the GstBuffer properties");
            GstClockTime pts = GST_BUFFER_PTS(buffer);
            GstClockTime dts = GST_BUFFER_DTS(buffer);
            GstClockTime duration = GST_BUFFER_DURATION(buffer);

            nlohmann::json properties;
            properties["pts"] = pts;
            properties["dts"] = dts;
            properties["duration"] = duration;

            ComPtr<IPayload> payload;
            CHECKHR(MessageBroker::CreatePayload(payload.AddressOf(), properties.dump().c_str()));
            CHECKHR(payload->SetCorrelationId(id.c_str()));
            CHECKHR(payload->SetTimestamp(now));

            CHECKHR(ctxt.Async ?
                _broker->PublishAsync(_buffer_properties_id.c_str(), payload) :
                _broker->Publish(_buffer_properties_id.c_str(), payload));
        }

        if(GetInterval() != 0)
        {
            _capture.Reset();
        }

        return hr;
    }

    ComPtr<IMessageBroker> _broker;
    int32_t _subscription_token = -1;
    ManualResetEvent _capture;
    CaptureContext _ctxt;
    std::string _subscription_id;
    Timestamp _interval_epoch = 0;
    std::string _meta;
    std::vector<MetaCaptureContext> _meta_ctxt;
    std::recursive_mutex _mtx;
    std::string _buffer_properties_id;

    GstElement* _parent = nullptr;
    bool _shutting_down = false;
};

// ================ GStreamer Boiler Plate Below ======================
G_BEGIN_DECLS

#define GST_TYPE_EMLCAPTURE   (gst_emlcapture_get_type())
#define GST_EMLCAPTURE(obj)   (G_TYPE_CHECK_INSTANCE_CAST((obj),GST_TYPE_EMLCAPTURE,GstEmlcapture))
#define GST_EMLCAPTURE_CLASS(klass)   (G_TYPE_CHECK_CLASS_CAST((klass),GST_TYPE_EMLCAPTURE,GstEmlcaptureClass))
#define GST_IS_EMLCAPTURE(obj)   (G_TYPE_CHECK_INSTANCE_TYPE((obj),GST_TYPE_EMLCAPTURE))
#define GST_IS_EMLCAPTURE_CLASS(obj)   (G_TYPE_CHECK_CLASS_TYPE((klass),GST_TYPE_EMLCAPTURE))
#define PLUGIN_DESCRIPTION "Plugin that captures GstBuffer and GstMeta data and sends to Message Broker"

typedef struct _GstEmlcapture GstEmlcapture;
typedef struct _GstEmlcaptureClass GstEmlcaptureClass;
struct _GstEmlcapture
{
    GstBaseTransform base_emlcapture;
    ComPtr<GstDataCapture> DataCapture;
};

struct _GstEmlcaptureClass
{
    GstBaseTransformClass base_emlcapture_class;
};

static GstElementClass *parent_class = NULL;
GType gst_emlcapture_get_type(void);

G_END_DECLS

GST_DEBUG_CATEGORY_STATIC(gst_emlcapture_debug_category);
#define GST_CAT_DEFAULT gst_emlcapture_debug_category

gboolean emlcapture_init(GstPlugin* emlcapture)
{
    return gst_element_register(emlcapture, "emlcapture", GST_RANK_NONE, GST_TYPE_EMLCAPTURE);
}

#ifndef VERSION
#define VERSION "1.0.0"
#endif
#ifndef PACKAGE
#define PACKAGE "emlcapture"
#endif
#ifndef PACKAGE_NAME
#define PACKAGE_NAME "emlcapture"
#endif
#ifndef GST_PACKAGE_ORIGIN
#define GST_PACKAGE_ORIGIN "aws"
#endif

GST_PLUGIN_DEFINE(GST_VERSION_MAJOR,
    GST_VERSION_MINOR,
    emlcapture,
    PLUGIN_DESCRIPTION,
    emlcapture_init, VERSION, "LGPL", PACKAGE_NAME, GST_PACKAGE_ORIGIN)

typedef enum
{
    PROP_0,
    SUBSCRIPTION_ID,
    BUFFER_MESSAGE_ID,
    INTERVAL,
    ASYNC,
    META,
    PROPERTIES
} PluginProperty;


void gst_emlcapture_set_property(GObject* object, guint property_id, const GValue* value, GParamSpec* pspec)
{
    GstEmlcapture* emlcapture = GST_EMLCAPTURE(object);
    switch (property_id) 
    {
        case PluginProperty::SUBSCRIPTION_ID:
            if(emlcapture->DataCapture != nullptr)
            {
                emlcapture->DataCapture->SetSubscriptionId(g_value_get_string(value));
            }

            break;
        case PluginProperty::BUFFER_MESSAGE_ID:
            if(emlcapture->DataCapture != nullptr)
            {
                emlcapture->DataCapture->SetBufferMessageId(g_value_get_string(value));
            }

            break;
        case PluginProperty::INTERVAL:
            if(emlcapture->DataCapture != nullptr)
            {
                emlcapture->DataCapture->SetInterval(g_value_get_int(value));
            }

            break;
        case PluginProperty::ASYNC:
            if(emlcapture->DataCapture != nullptr)
            {
                emlcapture->DataCapture->SetAsync(g_value_get_boolean(value));
            }

            break;
        case PluginProperty::META:
            if(emlcapture->DataCapture != nullptr)
            {
                emlcapture->DataCapture->SetMeta(g_value_get_string(value));
            }

            break;
        case PluginProperty::PROPERTIES:
            if(emlcapture->DataCapture != nullptr)
            {
                emlcapture->DataCapture->SetBufferPropertiesId(g_value_get_string(value));
            }

            break;
        default:
            break;
    }
}

void gst_emlcapture_get_property(GObject* object, guint property_id, GValue* value, GParamSpec* pspec)
{
    GstEmlcapture* emlcapture = GST_EMLCAPTURE(object);
    switch (property_id) 
    {
        case PluginProperty::SUBSCRIPTION_ID:
            if(emlcapture->DataCapture != nullptr)
            {
                g_value_set_string(value, emlcapture->DataCapture->GetSubscriptionId());
            }

            break;
        case PluginProperty::BUFFER_MESSAGE_ID:
            if(emlcapture->DataCapture != nullptr)
            {
                g_value_set_string(value, emlcapture->DataCapture->GetBufferMessageId());
            }

            break;
        case PluginProperty::INTERVAL:
            if(emlcapture->DataCapture != nullptr)
            {
                g_value_set_int(value, emlcapture->DataCapture->GetInterval());
            }

            break;
        case PluginProperty::ASYNC:
            if(emlcapture->DataCapture != nullptr)
            {
                g_value_set_boolean(value, emlcapture->DataCapture->GetAsync());
            }

            break;
        case PluginProperty::META:
            if(emlcapture->DataCapture != nullptr)
            {
                g_value_set_string(value, emlcapture->DataCapture->GetMeta());
            }

            break;
        case PluginProperty::PROPERTIES:
            if(emlcapture->DataCapture != nullptr)
            {
                g_value_set_string(value, emlcapture->DataCapture->GetBufferPropertiesId());
            }

            break;
        default:
            break;
    }
}

GstFlowReturn gst_emlcapture_chain(GstPad* pad, GstObject* parent, GstBuffer* buf)
{
    GstEmlcapture* emlcapture = GST_EMLCAPTURE(parent);
    if(emlcapture->DataCapture == nullptr)
    {
        GST_ERROR_OBJECT(emlcapture, "Plugin was not successfully initialized");
        return GST_FLOW_ERROR;
    }

    HRESULT hr = emlcapture->DataCapture->NewBuffer(buf);
    if(FAILED(hr))
    {
        GST_ERROR_OBJECT(emlcapture, "Error in emlcapture: %s", ErrorCodeToString(hr));
        return GST_FLOW_ERROR;
    }

    return gst_pad_push(emlcapture->base_emlcapture.srcpad, buf);
}

static GstStateChangeReturn gst_emlcapture_change_state(GstElement *element, GstStateChange transition) 
{
    // Set the interval epoch when transitioning to the playing state
    GstEmlcapture* capture = GST_EMLCAPTURE(element);
    if(transition == GST_STATE_CHANGE_PAUSED_TO_PLAYING)
    {
        if(capture->DataCapture != nullptr)
        {
            capture->DataCapture->SetIntervalEpoch();
        }
    }

    // Call the parent class's change_state method
    return GST_ELEMENT_CLASS(parent_class)->change_state(element, transition);
}

static GstStaticPadTemplate gst_emlcapture_src_template =
GST_STATIC_PAD_TEMPLATE("src",
    GST_PAD_SRC,
    GST_PAD_ALWAYS,
    GST_STATIC_CAPS("ANY")
);

static GstStaticPadTemplate gst_emlcapture_sink_template =
GST_STATIC_PAD_TEMPLATE("sink",
    GST_PAD_SINK,
    GST_PAD_ALWAYS,
    GST_STATIC_CAPS("ANY")
);

/* class initialization */
G_DEFINE_TYPE_WITH_CODE(GstEmlcapture, gst_emlcapture, GST_TYPE_BASE_TRANSFORM,
    GST_DEBUG_CATEGORY_INIT(gst_emlcapture_debug_category, "emlcapture", 0,
        "debug category for emlcapture element"));

void gst_emlcapture_dispose(GObject* object)
{
    GstEmlcapture* emlcapture = GST_EMLCAPTURE(object);
    GST_DEBUG_OBJECT(emlcapture, "dispose");

    /* clean up as possible.  may be called multiple times */
    G_OBJECT_CLASS(gst_emlcapture_parent_class)->dispose(object);
}

void gst_emlcapture_finalize(GObject* object)
{
    GstEmlcapture* emlcapture = GST_EMLCAPTURE(object);
    
    if(emlcapture->DataCapture != nullptr)
    {
        emlcapture->DataCapture.Release();
        emlcapture->DataCapture.Detach();
    }

    GST_DEBUG_OBJECT(emlcapture, "finalize");
    G_OBJECT_CLASS(gst_emlcapture_parent_class)->finalize(object);
}

void gst_emlcapture_class_init(GstEmlcaptureClass* klass)
{
    GObjectClass* gobject_class = (GObjectClass*)klass;
    GstElementClass* gstelement_class = (GstElementClass*)klass;

    gst_element_class_set_details_simple(gstelement_class,
        "emlcapture",
        "Pipeline Emlcapture 2",
        "Plugin that captures GstBuffer and GstMeta data and sends to Message Broker", "aws");

    gst_element_class_add_static_pad_template(gstelement_class, &gst_emlcapture_sink_template);
    gst_element_class_add_static_pad_template(gstelement_class, &gst_emlcapture_src_template);

    gobject_class->finalize = gst_emlcapture_finalize;
    gstelement_class->change_state = gst_emlcapture_change_state;
    parent_class = static_cast<GstElementClass*>(g_type_class_peek_parent(klass));

    /* Properties */
    gobject_class->set_property = gst_emlcapture_set_property;
    gobject_class->get_property = gst_emlcapture_get_property;

    g_object_class_install_property(gobject_class, PluginProperty::ASYNC,
        g_param_spec_boolean("async", "Asynchronous publish", "Flag indicating to publish messages to the Message Broker asynchronously or synchronously.  Default behavior is asynchronous (true).",
                         true, static_cast<GParamFlags>(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));

    g_object_class_install_property(gobject_class, PluginProperty::INTERVAL,
        g_param_spec_int("interval", "Capture Interval", "Value specifying the frequency, in milliseconds, that capture should take place.  To capture every frame set this value to 0 and to capture on demand set this value to less than 0.",
                         INT_MIN, INT_MAX, -1, static_cast<GParamFlags>(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));

    g_object_class_install_property(gobject_class, PluginProperty::SUBSCRIPTION_ID,
        g_param_spec_string("subscription-id", "Subscription Id", "Subscrpition id to receive capture commands.  If not specified then plugin will not register to remote commands",
                         "", static_cast<GParamFlags>(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));

    g_object_class_install_property(gobject_class, PluginProperty::BUFFER_MESSAGE_ID,
        g_param_spec_string("buffer-message-id", "Buffer Message Id", "The id of the message published to the Message Broker. ",
                         "", static_cast<GParamFlags>(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));

    g_object_class_install_property(gobject_class, PluginProperty::META,
        g_param_spec_string("meta", "GstMeta to capture", "Comma delimited list of the names of registered PayloadMeta objects that should be captured along with the id of the message to publish to the Message Broker.  Should take the form: impl1:id1,impl2:id2,impl3:id3,....",
                         "", static_cast<GParamFlags>(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));

    g_object_class_install_property(gobject_class, PluginProperty::PROPERTIES,
        g_param_spec_string("buffer-properties", "Properties about the GstBuffer.", "Publishes the GstBuffer PTS, DTS, and Duration of the buffer as a JSON string.  Request to have additional properties added to this list",
                         "", static_cast<GParamFlags>(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));
}

void gst_emlcapture_init(GstEmlcapture* emlcapture)
{
    gst_pad_set_chain_function(emlcapture->base_emlcapture.sinkpad, gst_emlcapture_chain);

    HRESULT hr = GstDataCapture::Create(emlcapture->DataCapture.AddressOf(), (GstElement*)emlcapture);
    if(FAILED(hr))
    {
        GST_ERROR_OBJECT(emlcapture, "Failed to intialize Capture plugin: %s", ErrorCodeToString(hr));
    }
}