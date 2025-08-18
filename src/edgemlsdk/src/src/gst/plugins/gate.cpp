/* GStreamer
 * Copyright (C) 2022 FIXME <fixme@example.com>
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Library General Public
 * License as published by the Free Software Foundation; either
 * version 2 of the License, or (at your option) any later version.
 *
 * This library is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
 * Library General Public License for more details.
 *
 * You should have received a copy of the GNU Library General Public
 * License along with this library; if not, write to the
 * Free Software Foundation, Inc., 51 Franklin Street, Suite 500,
 * Boston, MA 02110-1335, USA.
 */

#include <mutex>

#include <gst/gst.h>
#include <gst/base/gstbasetransform.h>

#include <nlohmann/json.hpp>

#include <Panorama/message_broker.h>
#include <Panorama/flowcontrol.h>
#include <app/device_env.h>
#include <misc.h>

using namespace Panorama;

G_BEGIN_DECLS

#define GST_TYPE_GATE   (gst_gate_get_type())
#define GST_GATE(obj)   (G_TYPE_CHECK_INSTANCE_CAST((obj),GST_TYPE_GATE,GstGate))
#define GST_GATE_CLASS(klass)   (G_TYPE_CHECK_CLASS_CAST((klass),GST_TYPE_GATE,GstGateClass))
#define GST_IS_GATE(obj)   (G_TYPE_CHECK_INSTANCE_TYPE((obj),GST_TYPE_GATE))
#define GST_IS_GATE_CLASS(obj)   (G_TYPE_CHECK_CLASS_TYPE((klass),GST_TYPE_GATE))

typedef struct _GstGate GstGate;
typedef struct _GstGateClass GstGateClass;
struct Filter; //forward declare
struct _GstGate
{
    GstBaseTransform base_gate;
    ComPtr<Filter> Filter;
    bool HonorPts;
    GstPad* go_to_pad;
    bool attempted_find_goto;
};

struct _GstGateClass
{
    GstBaseTransformClass base_gate_class;
};

static GstElementClass *parent_class = NULL;
GType gst_gate_get_type(void);

G_END_DECLS

GST_DEBUG_CATEGORY_STATIC(gst_gate_debug_category);
#define GST_CAT_DEFAULT gst_gate_debug_category

class Filter : public UnknownImpl<IUnknownAlias>
{
public:
    static HRESULT Create(Filter** ppObj, GstGate* parent)
    {
        HRESULT hr = S_OK;
        CREATE_COM(Filter, ptr);
        ptr->_parent = parent;
        *ppObj = ptr.Detach();
        return hr;
    }

    ~Filter()
    {
        COM_DTOR(Gate);
        if(_broker != nullptr)
        {
            _broker->Unsubscribe(_sub_token);
        }

        COM_DTOR_FIN(Gate);
    }

    bool AllowThrough()
    {
        std::lock_guard<std::mutex> lk(_mtx);
        if(_open)
        {
            return true;
        }

        if(_num_frames == 0)
        {
            return false;
        }

        _num_frames--;
        return true;
    }

    void SetOpen(bool val)
    {
        GST_INFO_OBJECT(_parent, "Setting open to %s", val ? "true" : "false");
        std::lock_guard<std::mutex> lk(_mtx);
        _open = val;
    }

    bool IsOpen()
    {
        std::lock_guard<std::mutex> lk(_mtx);
        return _open;
    }

    void SetNumAllowedFrames(int32_t val)
    {
        GST_INFO_OBJECT(_parent, "Setting allowed number of frames to %d", val);
        std::lock_guard<std::mutex> lk(_mtx);
        _num_frames = val;
    }

    int32_t GetRemainingFrames()
    {
        std::lock_guard<std::mutex> lk(_mtx);
        return _num_frames;
    }

    HRESULT SetSubscriptionId(const char* subscription_id)
    {
        HRESULT hr = S_OK;
        CHECKNULL_OR_EMPTY(subscription_id, E_INVALIDARG);

        // Current implementation only takes first command, doesn't resubscribe on property change
        // possible future improvement
        if(_subscription_id.empty())
        {
            _subscription_id = subscription_id;
            // Get the credential provider for this platform
            ComPtr<ICredentialProvider> credential_provider;
            CHECKHR(CreatePlatformCredentialProvider(credential_provider.AddressOf()));

            // Create the event broker
            CHECKHR(MessageBroker::Create(_broker.AddressOf(), credential_provider));
            CHECKHR(_broker->Initialize());

            hr = _broker->Subscribe(_subscription_id.c_str(), [&](IPayload* payload)
            {
                std::string command = payload->SerializeAsString();
                TraceInfo("Gate received command %s", command.c_str());
                if(nlohmann::json::accept(command) == false)
                {
                    GST_WARNING_OBJECT(_parent, "Command sent to gate plugin was not valid JSON");
                    return;
                }

                nlohmann::json rule = nlohmann::json::parse(command);
                if(ValidateJsonProperty<int32_t>(rule, "num_frames", false) == false)
                {
                    GST_WARNING_OBJECT(_parent, "Command not valid schema");
                    return;
                }

                if(ValidateJsonProperty<bool>(rule, "open", false) == false)
                {
                    GST_WARNING_OBJECT(_parent, "Command not valid schema");
                    return;
                }

                if(rule.contains("open"))
                {
                    this->SetOpen(rule["open"]);
                }
                
                // Only set the num_frames if the gate is closed
                // Unless desired behavior is to 'remember' the num_frames for when the gate is closed
                if(this->IsOpen() == false && rule.contains("num_frames"))
                {
                    this->SetNumAllowedFrames(rule["num_frames"]);
                }
            });
            CHECKHR(hr);
            _sub_token = hr;
        }

        return hr;
    }

    const char* GetSubscriptionId()
    {
        return _subscription_id.c_str();
    }

    HRESULT SetGoTo(const char* go_to)
    {
        CHECKNULL_OR_EMPTY(go_to, E_INVALIDARG);
        _go_to = go_to;
        return S_OK;
    }

    const char* GetGoTo()
    {
        return _go_to.c_str();
    }


private:
    std::string _subscription_id = "";
    int32_t _num_frames = 0;
    bool _open = true;
    GstGate* _parent = nullptr;
    std::mutex _mtx;
    ComPtr<IMessageBroker> _broker;
    int32_t _sub_token = 0;
    std::string _go_to;
};

// ================ GStreamer Boiler Plate Below ======================
typedef enum
{
    PROP_0,
    OPEN,
    NUM_FRAMES,
    SUBSCRIPTION_ID,
    HONOR_PTS,
    GO_TO
} PluginProperty;


void gst_gate_set_property(GObject* object, guint property_id, const GValue* value, GParamSpec* pspec)
{
    GstGate* gate = GST_GATE(object);

    switch (property_id) 
    {
        case PluginProperty::OPEN:
            if(gate->Filter != nullptr)
            {
                gate->Filter->SetOpen(g_value_get_boolean(value));
            }

            break;
        case PluginProperty::NUM_FRAMES:
            if(gate->Filter != nullptr)
            {
                gate->Filter->SetNumAllowedFrames(g_value_get_int(value));
            }

            break;
        case PluginProperty::SUBSCRIPTION_ID:
            if(gate->Filter != nullptr)
            {
                if(FAILED(gate->Filter->SetSubscriptionId(g_value_get_string(value))))
                {
                    GST_ERROR_OBJECT(gate, "Invalid ID for gate");
                }
            }

            break;
        case PluginProperty::HONOR_PTS:
            gate->HonorPts = g_value_get_boolean(value);
            break;
        case PluginProperty::GO_TO:
            if(gate->Filter != nullptr)
            {
                if(FAILED(gate->Filter->SetGoTo(g_value_get_string(value))))
                {
                    GST_ERROR_OBJECT(gate, "Invalid go-to for gate");
                }
            }

            break;
        default:
            G_OBJECT_WARN_INVALID_PROPERTY_ID(object, property_id, pspec);
            break;
    }
}

void gst_gate_get_property(GObject* object, guint property_id, GValue* value, GParamSpec* pspec)
{
    GstGate* gate = GST_GATE(object);
    switch (property_id) 
    {
        case PluginProperty::OPEN:
            if(gate->Filter != nullptr)
            {
                g_value_set_boolean(value, gate->Filter->IsOpen());
            }

            break;
        case PluginProperty::NUM_FRAMES:
            if(gate->Filter != nullptr)
            {
                g_value_set_int(value, gate->Filter->GetRemainingFrames());
            }

            break;
        case PluginProperty::SUBSCRIPTION_ID:
            if(gate->Filter != nullptr)
            {
                g_value_set_string(value, gate->Filter->GetSubscriptionId());
            }

            break;
        case PluginProperty::HONOR_PTS:
            g_value_set_boolean(value, gate->HonorPts);
            break;
        case PluginProperty::GO_TO:
            if(gate->Filter != nullptr)
            {
                g_value_set_string(value, gate->Filter->GetGoTo());
            }

            break;
        default:
            G_OBJECT_WARN_INVALID_PROPERTY_ID(object, property_id, pspec);
            break;
    }
}

void find_goto_pad(GstGate* gate, GstBin *bin) {
    GValue item = G_VALUE_INIT;
    gboolean done = FALSE;

    GstIterator* it = gst_bin_iterate_elements(bin);
    while (done == FALSE) 
    {
        switch (gst_iterator_next(it, &item)) 
        {
            case GST_ITERATOR_OK:
            {
                GstElement* element = GST_ELEMENT(g_value_get_object(&item));
                std::string name = GST_ELEMENT_NAME(element);
                if(name.compare(gate->Filter->GetGoTo()) == 0)
                {
                    TraceInfo("Source pad to jump to has been located");
                    gate->go_to_pad = gst_element_get_static_pad(element, "src");
                    done = TRUE;
                }

                g_value_reset(&item);
                break;
            }
            case GST_ITERATOR_RESYNC:
                gst_iterator_resync(it);
                break;
            case GST_ITERATOR_ERROR:
            case GST_ITERATOR_DONE:
                done = TRUE;
                break;
        }
    }

    g_value_unset(&item);
    gst_iterator_free(it);
}

static GstStateChangeReturn gst_gate_change_state(GstElement *element, GstStateChange transition) 
{
    // If going from the null to ready state
    // we need to allow the first frame through to allow entire pipeline to enter playing state
    GstGate* gate = GST_GATE(element);

    if(gate->Filter == nullptr)
    {
        GST_ERROR_OBJECT(gate, "Gate plugin is not in a valid state, Filter was not initialized successfully");
        return GST_STATE_CHANGE_FAILURE;
    }

    // Get the srcpad to push the buffer onto if go-to property is specified
    // Couldn't find a better place to put this as init the parent hasn't been set yet
    // Hence the need for the attempted flag, as to only run this the first time
    if(gate->attempted_find_goto == false && strlen(gate->Filter->GetGoTo()) > 0)
    {
        gate->attempted_find_goto = true;
        // Find the pipeline holding this plugin
        GstObject* parent = gst_element_get_parent(element);
        while (parent && !GST_IS_PIPELINE(parent)) 
        {
            GstObject* grandparent = gst_object_get_parent(parent);
            gst_object_unref(parent);
            parent = grandparent;
        }

        if(parent != nullptr)
        {
            find_goto_pad(gate, GST_BIN(parent));
            if(gate->go_to_pad == nullptr)
            {
                GST_ERROR_OBJECT(gate, "Could not retrieve the source pad of element to jump to");
                gst_object_unref(parent);
                return GST_STATE_CHANGE_FAILURE;
            }

            gst_object_unref(parent);
        }
    }

    return GST_ELEMENT_CLASS(parent_class)->change_state(element, transition);
}

GstFlowReturn gst_gate_chain(GstPad* pad, GstObject* parent, GstBuffer* buf)
{
    static uint64_t base_pts = 0;
    static uint64_t prev_pts = 0;

    GstGate* gate = GST_GATE(parent);
    if(gate->Filter == nullptr)
    {
        GST_ERROR_OBJECT(gate, "Plugin was not successfully initialized");
        return GST_FLOW_ERROR;
    }

    if(gate->Filter->AllowThrough())
    {
        return gst_pad_push(gate->base_gate.srcpad, buf);
    }
    else if(gate->go_to_pad != nullptr)
    {
        // gate is 'closed' but we want to skip ahead in the pipeline (likely to a sink plugin)
        return gst_pad_push(gate->go_to_pad, buf);
    }
    else
    {
        if(gate->HonorPts)
        {
            uint64_t pts = GST_BUFFER_PTS(buf);
            if(pts != GST_CLOCK_TIME_NONE)
            {
                GstElement *pipeline = GST_ELEMENT(GST_ELEMENT_PARENT(GST_ELEMENT(parent)));
                GstClock* clock = gst_element_get_clock(pipeline);
                
                if (clock) 
                {
                    if(base_pts == 0)
                    {
                        // the clock isn't created until transitioning from READY to PAUSED
                        // so the clock_get_time and get_base_time return with a reference to that point in time
                        // However, src plugins (like videotestsrc) are already sending buffers and incrementing the PTS
                        // while in the READY state.
                        // When computing the time to sleep we need to remove the accumulated PTS occured before the clock was created 
                        base_pts = prev_pts;
                    }

                    uint64_t elapsed_time = gst_clock_get_time(clock) - gst_element_get_base_time(pipeline);
                    gst_object_unref(clock);

                    int32_t sleep_ms = static_cast<float>(pts - base_pts - elapsed_time) * 0.000001f;
                    if(sleep_ms > 0)
                    {
                        ThreadSleep(sleep_ms); 
                    }
                }
            }

            prev_pts = pts;
        }
    }

    gst_buffer_unref(buf);
    return GST_FLOW_OK;
}

static GstStaticPadTemplate gst_gate_src_template =
GST_STATIC_PAD_TEMPLATE("src",
    GST_PAD_SRC,
    GST_PAD_ALWAYS,
    GST_STATIC_CAPS("ANY")
);

static GstStaticPadTemplate gst_gate_sink_template =
GST_STATIC_PAD_TEMPLATE("sink",
    GST_PAD_SINK,
    GST_PAD_ALWAYS,
    GST_STATIC_CAPS("ANY")
);

/* class initialization */
G_DEFINE_TYPE_WITH_CODE(GstGate, gst_gate, GST_TYPE_BASE_TRANSFORM,
    GST_DEBUG_CATEGORY_INIT(gst_gate_debug_category, "gate", 0,
        "debug category for gate element"));

void gst_gate_dispose(GObject* object)
{
    GstGate* gate = GST_GATE(object);
    GST_DEBUG_OBJECT(gate, "dispose");

    /* clean up as possible.  may be called multiple times */
    G_OBJECT_CLASS(gst_gate_parent_class)->dispose(object);
}

void gst_gate_finalize(GObject* object)
{
    GstGate* gate = GST_GATE(object);
    
    if(gate->go_to_pad != nullptr)
    {
        gst_object_unref(gate->go_to_pad);
    }

    if(gate->Filter != nullptr)
    {
        gate->Filter.Release();
        gate->Filter.Detach();
    }

    GST_DEBUG_OBJECT(gate, "finalize");

    /* clean up object here */
    G_OBJECT_CLASS(gst_gate_parent_class)->finalize(object);
}

void gst_gate_class_init(GstGateClass* klass)
{
    GObjectClass* gobject_class;
    GstElementClass* gstelement_class;

    gobject_class = (GObjectClass*)klass;
    gstelement_class = (GstElementClass*)klass;
    parent_class = static_cast<GstElementClass*>(g_type_class_peek_parent(klass));
    

    gst_element_class_set_details_simple(gstelement_class,
        "gate",
        "Pipeline Gate",
        "Plugin that acts as a conditional stop for messages through the pipeline", "aws");

    gst_element_class_add_static_pad_template(gstelement_class, &gst_gate_sink_template);
    gst_element_class_add_static_pad_template(gstelement_class, &gst_gate_src_template);

    gobject_class->finalize = gst_gate_finalize;
    gstelement_class->change_state = gst_gate_change_state;

    /* Properties */
    gobject_class->set_property = gst_gate_set_property;
    gobject_class->get_property = gst_gate_get_property;

    g_object_class_install_property(gobject_class, PluginProperty::OPEN,
        g_param_spec_boolean("open", "Gate Open", "Set to true to allow frames to pass, false will stop flow",
                         true, static_cast<GParamFlags>(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));

    g_object_class_install_property(gobject_class, PluginProperty::NUM_FRAMES,
        g_param_spec_int("numframes", "Number of Frames", "Sets the number of frames that will be allowed before the gate is closed again, property 'open' takes precedence",
                         0, INT_MAX, 0, static_cast<GParamFlags>(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));

    g_object_class_install_property(gobject_class, PluginProperty::SUBSCRIPTION_ID,
        g_param_spec_string("subscription-id", "Remote Command Topic", "Topic for which this plugin will register to for remote commands.  If not specified then plugin will not register to remote commands",
                         "", static_cast<GParamFlags>(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));

    g_object_class_install_property(gobject_class, PluginProperty::HONOR_PTS,
        g_param_spec_boolean("honor-pts", "Honor Presentation Time", "Setting to true will cause this plugin to sleep until the time the buffer should be presented.  Setting to false will incur no sleep.  Default is true.",
                         true, static_cast<GParamFlags>(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));

    g_object_class_install_property(gobject_class, PluginProperty::GO_TO,
        g_param_spec_string("go-to", "Where to jump in the pipeline", 
                        "The name of the plugin whose source pad you want this gate to push to.  Useful when using a gate after a tee to not block all paths."
                        "WARNING: pad negotiation is not respected."
                        "Recommended to set this to a plugin immediately before a fakesink, otherwise use with care."
                        "If not set then the gate will block flow through the entire pipeline (may or may not be desired)"
                        "Default is empty",
                         "", static_cast<GParamFlags>(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));
}

void gst_gate_init(GstGate* gate)
{
    gst_pad_set_chain_function(gate->base_gate.sinkpad, gst_gate_chain);

    gate->HonorPts = true;
    gate->go_to_pad = nullptr;
    gate->attempted_find_goto = false;

    HRESULT hr = Filter::Create(gate->Filter.AddressOf(), gate);
    if(FAILED(hr))
    {
        GST_ERROR_OBJECT(gate, "Failed to intialize Gate plugin: %s", ErrorCodeToString(hr));
    }
}

gboolean gate_init(GstPlugin* gate)
{
    return gst_element_register(gate, "gate", GST_RANK_NONE,
        GST_TYPE_GATE);
}

#ifndef VERSION
#define VERSION "1.0.0"
#endif
#ifndef PACKAGE
#define PACKAGE "gate"
#endif
#ifndef PACKAGE_NAME
#define PACKAGE_NAME "gate"
#endif
#ifndef GST_PACKAGE_ORIGIN
#define GST_PACKAGE_ORIGIN "aws"
#endif

GST_PLUGIN_DEFINE(GST_VERSION_MAJOR,
    GST_VERSION_MINOR,
    gate,
    "Plugin that acts as a conditional stop for messages through the pipeline",
    gate_init, VERSION, "LGPL", PACKAGE_NAME, GST_PACKAGE_ORIGIN)