#include <mutex>
#include <limits.h>

#include <gst/gst.h>
#include <gst/base/gstbasetransform.h>

#include <nlohmann/json.hpp>

#include <Panorama/message_broker.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/eventing.h>
#include <Panorama/gst.h>
#include <Panorama/videocapture.h>
#include <app/device_env.h>
#include <misc.h>
#include <scheduling.h>
#include <queue>

using namespace Panorama;

class GstVideoCapture
{
public:
    GstVideoCapture() 
    {
        _async = true;
        _width = -1;
        _height = -1;
        _max_length = -1;
        _subscribe_token = -1;
        _offset = 0;
    };

    ~GstVideoCapture()
    {
        if(_subscribe_token >= 0)
        {
            PEEKHR(_broker->Unsubscribe(_subscribe_token));
        }
    }

    HRESULT Initialize()
    {
        HRESULT hr = S_OK;
        CHECKIF(_max_length <= 0, E_OUTOFRANGE);
        CHECKIF(_width <= 0, E_OUTOFRANGE);
        CHECKIF(_height <= 0, E_OUTOFRANGE);

        // Get the credential provider for this platform
        ComPtr<ICredentialProvider> credential_provider;
        CHECKHR(CreatePlatformCredentialProvider(credential_provider.AddressOf()));

        // Create the message broker
        CHECKHR(MessageBroker::Create(_broker.AddressOf(), credential_provider));
        CHECKHR(_broker->Initialize());

        CHECKHR(VideoCapture::Create(_capture.AddressOf(), _max_length, _width, _height, Encoding::H264, ContainerFormat::MP4));
        TraceVerbose("Video capture successfully Initialized");

        // Subscribe (if set)
        if(_subscription_id.empty() == false)
        {
            _broker->Subscribe(_subscription_id.c_str(), [&](IPayload* payload)
            {
                TraceVerbose("Received command to capture video");
                CHECKIF_MSG(nlohmann::json::accept(payload->SerializeAsString()) == false,, "Command was not in json format");
                nlohmann::json jObj = nlohmann::json::parse(payload->SerializeAsString());

                CHECKIF_MSG(ValidateJsonProperty<int32_t>(jObj, "clip_length", false) == false,, "clip_length is not an integer");
                CHECKIF_MSG(ValidateJsonProperty<int64_t>(jObj, "timestamp", false) == false,, "timestamp is not an integer");
                CHECKIF_MSG(ValidateJsonProperty<float>(jObj, "pos", false) == false,, "pos is not a float");

                int32_t clip_length = jObj.contains("clip_length") ? static_cast<int32_t>(jObj["clip_length"]) : INT_MAX;
                int64_t ref_ts = jObj.contains("timestamp") ? static_cast<int64_t>(jObj["timestamp"]) : -1;
                float pos = jObj.contains("pos") ? static_cast<float>(jObj["pos"]) : 0.0f;

                TraceInfo("Clip Length = %d ms", clip_length);
                TraceInfo("Timestamp = %lld", ref_ts);
                TraceInfo("Position = %f", pos);

                if(ref_ts >= 0)
                {
                    GstClock *system_clock = gst_system_clock_obtain();
                    GstClockTime current_time = gst_clock_get_time(system_clock);
                    gst_object_unref(system_clock);
                    
                    double time_til = (1.0 - static_cast<double>(pos)) * static_cast<double>(_max_length); // where in the video
                    if(ref_ts >= current_time)
                    {
                        time_til +=  static_cast<double>(ref_ts - current_time) / 1000000.0; // time from now
                    }

                    time_til /= 1000.0f; // convert to seconds

                    TraceInfo("Queing Clip Generation for %" GST_TIME_FORMAT "@%f, approximately %lf seconds from now", GST_TIME_ARGS(ref_ts), pos, time_til);
                    ref_ts -= _offset;
                }

                _capture->GenerateMultipleClipsAsync(clip_length, ref_ts, pos, [&](IVideoClipCollection* collection, bool success)
                {
                    CHECKIF(_message_id.empty(),);

                    ComPtr<IBatchPayload> batch_payload;
                    CHECK_FAIL(MessageBroker::CreateBatchPayload(batch_payload.AddressOf()), );

                    for(int32_t idx = 0; idx < collection->Count(); idx++)
                    {
                        ComPtr<IVideoClip> clip = collection->GetClip(idx);
                        CHECKNULL(clip, );
                        ComPtr<IVideoPayload> payload;
                        CHECK_FAIL(VideoCapture::VideoPayload(payload.AddressOf(), clip),);
                        CHECK_FAIL(payload->SetTimestamp(clip->StartPTS() + _offset),);
                        CHECK_FAIL(batch_payload->AddPayload(payload),);
                    }

                    CHECK_FAIL(_async ?
                                    _broker->PublishAsync(_message_id.c_str(), batch_payload, nullptr) :
                                    _broker->Publish(_message_id.c_str(), batch_payload),);
                });
            });

            _subscribe_token = hr;
            hr = S_OK;
            TraceVerbose("Subscribed to id %s", _subscription_id.c_str());
        }

        return hr;
    }

    HRESULT AddFrame(IBuffer* data, int64_t pts, int64_t dts, int64_t duration)
    {
        return _capture->AddFrame(data, pts, dts, duration);
    }

    const char* GetSubscriptionId()
    {
        return _subscription_id.c_str();
    }

    void SetSubscriptionId(const char* subscription_id)
    {
        _subscription_id = subscription_id;
    }

    const char* GetMessageId()
    {
        return _message_id.c_str();
    }

    void SetMessageId(const char* message_id)
    {
        _message_id = message_id;
    }

    int GetMaxLength()
    {
        return _max_length;
    }

    void SetMaxLength(int32_t max_length)
    {
        _max_length = max_length;
    }

    bool GetAsync()
    {
        return _async;
    }

    void SetAsync(bool async)
    {
        _async = async;
    }

    void SetWidth(int32_t width)
    {
        _width = width;
    }

    void SetHeight(int32_t height)
    {
        _height = height;
    }

    void SetOffset(int64_t offset)
    {
        _offset = offset;
    }

private:
    std::string _subscription_id;
    std::string _message_id;
    int32_t _max_length, _width, _height;
    ComPtr<IVideoCapture> _capture;

    ComPtr<IMessageBroker> _broker;
    int32_t _subscribe_token;
    bool _async;
    int64_t _offset;
};

// ================ GStreamer Boiler Plate Below ======================
G_BEGIN_DECLS

#define GST_TYPE_EMLVIDEOCAPTURE   (gst_emlvideocapture_get_type())
#define GST_EMLVIDEOCAPTURE(obj)   (G_TYPE_CHECK_INSTANCE_CAST((obj),GST_TYPE_EMLVIDEOCAPTURE,GstEmlvideocapture))
#define GST_EMLVIDEOCAPTURE_CLASS(klass)   (G_TYPE_CHECK_CLASS_CAST((klass),GST_TYPE_EMLVIDEOCAPTURE,GstEmlvideocaptureClass))
#define GST_IS_EMLVIDEOCAPTURE(obj)   (G_TYPE_CHECK_INSTANCE_TYPE((obj),GST_TYPE_EMLVIDEOCAPTURE))
#define GST_IS_EMLVIDEOCAPTURE_CLASS(obj)   (G_TYPE_CHECK_CLASS_TYPE((klass),GST_TYPE_EMLVIDEOCAPTURE))
#define PLUGIN_DESCRIPTION "Plugin used to capture a rolling video from the video stream"
#define PLUGIN_LONG_NAME "EdgeML-SDK Video Capture"
#define PLUGIN_CLASSIFICATION "Transform/Video"
#define PLUGIN_AUTHOR "EdgeML-SDK <todo: alias>"

#ifndef VERSION
#define VERSION "1.0.0"
#endif
#ifndef PACKAGE
#define PACKAGE "emlvideocapture"
#endif
#ifndef PACKAGE_NAME
#define PACKAGE_NAME "emlvideocapture"
#endif
#ifndef GST_PACKAGE_ORIGIN
#define GST_PACKAGE_ORIGIN "https://alpha.www.docs.aws.a2z.com/edgeml-sdk/v1/1.0/index.html"
#endif

typedef struct _GstEmlvideocapture GstEmlvideocapture;
typedef struct _GstEmlvideocaptureClass GstEmlvideocaptureClass;
struct _GstEmlvideocapture
{
    GstBaseTransform base_emlvideocapture;
    GstVideoCapture* recorder;
    bool offset_computed;
};

struct _GstEmlvideocaptureClass
{
    GstBaseTransformClass base_emlvideocapture_class;
};

static GstElementClass *parent_class = NULL;
GType gst_emlvideocapture_get_type(void);

G_END_DECLS

GST_DEBUG_CATEGORY_STATIC(gst_emlvideocapture_debug_category);
#define GST_CAT_DEFAULT gst_emlvideocapture_debug_category

gboolean emlvideocapture_init(GstPlugin* emlvideocapture)
{
    return gst_element_register(emlvideocapture, "emlvideocapture", GST_RANK_NONE, GST_TYPE_EMLVIDEOCAPTURE);
}

GST_PLUGIN_DEFINE(GST_VERSION_MAJOR,
    GST_VERSION_MINOR,
    emlvideocapture,
    PLUGIN_DESCRIPTION,
    emlvideocapture_init, VERSION, "Proprietary", PACKAGE_NAME, GST_PACKAGE_ORIGIN)

typedef enum
{
    PROP_0,
    SUBSCRIPTION_ID,
    MESSAGE_ID,
    MAX_LENGTH,
    ASYNC
} PluginProperty;

void gst_emlvideocapture_set_property(GObject* object, guint property_id, const GValue* value, GParamSpec* pspec)
{
    GstEmlvideocapture* emlvideocapture = GST_EMLVIDEOCAPTURE(object);

    switch (property_id) 
    {
        case PluginProperty::SUBSCRIPTION_ID:
            emlvideocapture->recorder->SetSubscriptionId(g_value_get_string(value));
            break;
        case PluginProperty::MESSAGE_ID:
            emlvideocapture->recorder->SetMessageId(g_value_get_string(value));
            break;
        case PluginProperty::MAX_LENGTH:
            emlvideocapture->recorder->SetMaxLength(g_value_get_int(value));
            break;
        case PluginProperty::ASYNC:
            emlvideocapture->recorder->SetAsync(g_value_get_boolean(value));
            break;
    }
}

void gst_emlvideocapture_get_property(GObject* object, guint property_id, GValue* value, GParamSpec* pspec)
{
    GstEmlvideocapture* emlvideocapture = GST_EMLVIDEOCAPTURE(object);

    switch (property_id) 
    {
        case PluginProperty::SUBSCRIPTION_ID:
            g_value_set_string(value, emlvideocapture->recorder->GetSubscriptionId());
            break;
        case PluginProperty::MESSAGE_ID:
            g_value_set_string(value, emlvideocapture->recorder->GetMessageId());
            break;
        case PluginProperty::MAX_LENGTH:
            g_value_set_int(value, emlvideocapture->recorder->GetMaxLength());
            break;
        case PluginProperty::ASYNC:
            g_value_set_boolean(value, emlvideocapture->recorder->GetAsync());
            break;
    }
}

GstFlowReturn gst_emlvideocapture_chain(GstPad* pad, GstObject* parent, GstBuffer* buf)
{
    HRESULT hr = S_OK;
    GstEmlvideocapture* emlvideocapture = GST_EMLVIDEOCAPTURE(parent);
    
    // Create IBuffer from GstBuffer
    ComPtr<IBuffer> buffer;
    GstMapInfo map;
    gst_buffer_map(buf, &map, GST_MAP_READ);
    CHECK_FAIL(Buffer::Create(buffer.AddressOf(), map.size), GstFlowReturn::GST_FLOW_ERROR);
    memcpy(buffer->Data(), map.data, map.size);
    gst_buffer_unmap(buf, &map);

    // Get the GstBuffer metadata
    GstClockTime pts = GST_BUFFER_PTS(buf);
    GstClockTime dts = GST_BUFFER_DTS(buf);
    GstClockTime duration = GST_BUFFER_DURATION(buf);

    GstFormat format = GST_FORMAT_TIME;
    gint64 position = 0;

    // Some encoders (looking at you x264enc) will offset the PTS/DTS to avoid negative numbers
    // So, the PTS that we receive here are not aligned with the system clock.
    // Compute the offset from the first PTS to the current system time so we can
    // transform between the two coordinate frames.
    if(emlvideocapture->offset_computed == false)
    {
        GstClock *system_clock = gst_system_clock_obtain();
        GstClockTime current_time = gst_clock_get_time(system_clock);
        gst_object_unref(system_clock);
        emlvideocapture->recorder->SetOffset(current_time - pts);
    }

    // Add IBuffer to recorder
    CHECK_FAIL(emlvideocapture->recorder->AddFrame(buffer, pts, dts, duration), GstFlowReturn::GST_FLOW_ERROR);
    return gst_pad_push(emlvideocapture->base_emlvideocapture.srcpad, buf);
}

static GstStateChangeReturn gst_emlvideocapture_change_state(GstElement *element, GstStateChange transition) 
{
    // Set the interval epoch when transitioning to the playing state
    GstEmlvideocapture* capture = GST_EMLVIDEOCAPTURE(element);

    // Call the parent class's change_state method
    return GST_ELEMENT_CLASS(parent_class)->change_state(element, transition);
}

static gboolean emlvideocapture_event_function(GstPad *pad, GstObject *parent, GstEvent *event) 
{
    HRESULT hr = S_OK;
    gboolean ret;
    GstStructure *structure;
    gint width, height;
    GstEmlvideocapture* capture = GST_EMLVIDEOCAPTURE(parent);
    const gchar* format;

    switch (GST_EVENT_TYPE(event)) 
    {
        case GST_EVENT_CAPS:
            GstCaps *caps;

            // Parse the caps from the event
            gst_event_parse_caps(event, &caps);
            if(caps == nullptr)
            {
                GST_ERROR_OBJECT(parent, "Could not parse caps");
                return false;
            }

            structure = gst_caps_get_structure(caps, 0);
            if(gst_structure_get_int(structure, "width", &width) == false)
            {
                GST_ERROR_OBJECT(parent, "Could not get width from caps");
                return false;
            }

            if(gst_structure_get_int(structure, "height", &height) == false)
            {
                GST_ERROR_OBJECT(parent, "Could not get width from caps");
                return false;
            }

            format = gst_structure_get_string(structure, "format");

            capture->recorder->SetWidth(width);
            capture->recorder->SetHeight(height);
            CHECKIF_MSG(capture->recorder->Initialize(), false,  "Failed to initialize video capture");
            gst_caps_unref(caps);
            return true;
        default:
            // For other events, call the default event handler
            ret = gst_pad_event_default(pad, parent, event);
            break;
    }

    return ret;
}

static GstStaticPadTemplate gst_emlvideocapture_src_template =
GST_STATIC_PAD_TEMPLATE("src",
    GST_PAD_SRC,
    GST_PAD_ALWAYS,
    GST_STATIC_CAPS("video/x-h264,stream-format=(string)byte-stream")
);

static GstStaticPadTemplate gst_emlvideocapture_sink_template =
GST_STATIC_PAD_TEMPLATE("sink",
    GST_PAD_SINK,
    GST_PAD_ALWAYS,
    GST_STATIC_CAPS("video/x-h264,stream-format=(string)byte-stream")
);

/* class initialization */
G_DEFINE_TYPE_WITH_CODE(GstEmlvideocapture, gst_emlvideocapture, GST_TYPE_BASE_TRANSFORM,
    GST_DEBUG_CATEGORY_INIT(gst_emlvideocapture_debug_category, "emlvideocapture", 0,
        "debug category for emlvideocapture element"));

void gst_emlvideocapture_dispose(GObject* object)
{
    GstEmlvideocapture* emlvideocapture = GST_EMLVIDEOCAPTURE(object);
    GST_DEBUG_OBJECT(emlvideocapture, "dispose");

    /* clean up as possible.  may be called multiple times */
    G_OBJECT_CLASS(gst_emlvideocapture_parent_class)->dispose(object);
}

void gst_emlvideocapture_finalize(GObject* object)
{
    GstEmlvideocapture* emlvideocapture = GST_EMLVIDEOCAPTURE(object);
    if(emlvideocapture->recorder != nullptr)
    {
        delete emlvideocapture->recorder;
        emlvideocapture->recorder = nullptr;
    }

    GST_DEBUG_OBJECT(emlvideocapture, "finalize");
    G_OBJECT_CLASS(gst_emlvideocapture_parent_class)->finalize(object);
}

void gst_emlvideocapture_class_init(GstEmlvideocaptureClass* klass)
{
    GObjectClass* gobject_class = (GObjectClass*)klass;
    GstElementClass* gstelement_class = (GstElementClass*)klass;

    gst_element_class_set_details_simple(gstelement_class,
        PLUGIN_LONG_NAME,
        PLUGIN_CLASSIFICATION,
        PLUGIN_DESCRIPTION, 
        PLUGIN_AUTHOR);

    gst_element_class_add_static_pad_template(gstelement_class, &gst_emlvideocapture_sink_template);
    gst_element_class_add_static_pad_template(gstelement_class, &gst_emlvideocapture_src_template);

    gobject_class->finalize = gst_emlvideocapture_finalize;
    gstelement_class->change_state = gst_emlvideocapture_change_state;
    parent_class = static_cast<GstElementClass*>(g_type_class_peek_parent(klass));

    /* Properties */
    gobject_class->set_property = gst_emlvideocapture_set_property;
    gobject_class->get_property = gst_emlvideocapture_get_property;

    g_object_class_install_property(gobject_class, PluginProperty::SUBSCRIPTION_ID,
        g_param_spec_string("subscription-id", "Remote Command Topic", "Topic for which this plugin will register to for remote commands.  If not specified then plugin will not register to remote commands",
                         "", static_cast<GParamFlags>(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));

    g_object_class_install_property(gobject_class, PluginProperty::MESSAGE_ID,
        g_param_spec_string("message-id", "Buffer Message Id", "The id of the message published to the Message Broker. ",
                         "", static_cast<GParamFlags>(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));

    g_object_class_install_property(gobject_class, PluginProperty::MAX_LENGTH,
        g_param_spec_int("max-length", "Maximum length (ms)", "The maximum amount of video, in miliseconds, to store in memory.",
                         0, INT_MAX, 0, static_cast<GParamFlags>(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));

    g_object_class_install_property(gobject_class, PluginProperty::ASYNC,
        g_param_spec_boolean("async", "Asynchronous publish", "Flag indicating to publish messages to the Message Broker asynchronously or synchronously.  Default behavior is asynchronous (true).",
                         true, static_cast<GParamFlags>(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));
}

void gst_emlvideocapture_init(GstEmlvideocapture* emlvideocapture)
{
    /* Set Initial Values*/
    emlvideocapture->recorder = new GstVideoCapture();
    emlvideocapture->offset_computed = false;

    /* Define transform */
    gst_pad_set_chain_function(emlvideocapture->base_emlvideocapture.sinkpad, gst_emlvideocapture_chain);

    /* caps */
    gst_pad_set_event_function(emlvideocapture->base_emlvideocapture.sinkpad, emlvideocapture_event_function);
}