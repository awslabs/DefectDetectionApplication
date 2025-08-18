#include <thread>
#include <queue>
#include <stdexcept>

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

struct ElementData
{
    // GstClockTime is an int representing nanoseconds
    std::queue<GstClockTime> startTimes;
    
    // nanoseconds
    double mean = 0;
    double m2 = 0;

    int count = 0;
};

class ElementLatency : public UnknownImpl<IUnknownAlias>
{
public:
    static HRESULT Create(ElementLatency** ppObj, int32_t accumulationTime)
    {
        COM_FACTORY(ElementLatency, Initialize(accumulationTime));
    }

    ~ElementLatency()
    {
        COM_DTOR(ElementLatency);
        _shutdown = true;
        _sampleEvt.Set();
        if(_reportingThread.joinable())
        {
            _reportingThread.join();
        }
        COM_DTOR_FIN(ElementLatency);
    }

    /**
     * Updates the start times of the receiving element and calculates the latency for the sending element.
    */
    void CalculateLatency(const std::string &prevElementName, const std::string &nextElementName, GstClockTime ts)
    {
        // Edge case: event happening from source -> sink element (2 element pipeline)
        // Edge case: event happening from source element -> next element
        // Edge case: event happening from prev element -> sink element
        // Edge case: element has more than 1 buffer pushed to it, before it pushes a buffer out.
        // Edge case: element pushes more than 1 buffer out, before it has another buffer pushed to it.

        std::unique_lock<std::mutex> lck(_elementDataMapMutex);
        auto prevIt = _elementDataMap.find(prevElementName);
        auto nextIt = _elementDataMap.find(nextElementName);

        if (nextIt != _elementDataMap.end())
        {
            // Update the start times of the next element, since it is receiving data.
            nextIt->second.startTimes.push(ts);
        }
        
        if (prevIt != _elementDataMap.end())
        {
            auto &prevElement = prevIt->second;

            // An element should never be able to push a buffer if it hasn't previously received one.
            // Extreme edge case safety where an element receives a buffer list but outputs single buffers.
            if (!prevElement.startTimes.empty())
            {
                // Assuming that buffers are processed in the order that they were received.
                GstClockTime startTime = prevElement.startTimes.front();
                prevElement.startTimes.pop();
                
                // New timestamp should always be after the start time, but just for safety.
                if (ts > startTime)
                {
                    // Calculate data for the previous element, using Welford's algorithm: https://en.wikipedia.org/wiki/Algorithms_for_calculating_variance#Welford's_online_algorithm
                    // We keep a running tally rather than storing a list and iterating through at reporting time in the name of efficiency.
                    prevElement.count++;
                    auto newTime = ts - startTime;
                    double delta = newTime - prevElement.mean;
                    prevElement.mean += (delta / prevElement.count);
                    double delta2 = newTime - prevElement.mean;
                    prevElement.m2 += (delta * delta2);
                }
            }
        }
    }

    /**
     * Adds the element to the map
    */
    void RegisterElement(const std::string &name)
    {
        std::unique_lock<std::mutex> lck(_elementDataMapMutex);
        if (_elementDataMap.find(name) == _elementDataMap.end())
        {
            _elementDataMap[name] = ElementData();
        }
    }

    /**
     * Removes the element from the map
    */
    void DeregisterElement(const std::string &name)
    {
        std::unique_lock<std::mutex> lck(_elementDataMapMutex);
        auto it = _elementDataMap.find(name);
        if (it != _elementDataMap.end())
        {
            _elementDataMap.erase(it);
        }
    }

private:
    HRESULT Initialize(int32_t accumulationTime)
    {
        HRESULT hr = S_OK;

        // Get the credential provider or this platform
        ComPtr<ICredentialProvider> credential_provider;
        CHECKHR(CreatePlatformCredentialProvider(credential_provider.AddressOf()));

        // Create the message broker
        CHECKHR(MessageBroker::Create(_broker.AddressOf(), credential_provider));
        CHECKHR(_broker->Initialize());

        // Setup thread that computes the latency
        _accumulationTime = accumulationTime;
        _shutdown = false;

        _reportingThread = std::thread([&]()
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
                    std::unique_lock<std::mutex> lck(_elementDataMapMutex);

                    for(auto iter = _elementDataMap.begin(); iter != _elementDataMap.end(); iter++)
                    {
                        nlohmann::json elementObj;

                        // If no data was received this reporting cycle
                        if (iter->second.count == 0)
                        {
                            elementObj["mean"] = -1;
                            elementObj["variance"] = -1;
                            elementObj["count"] = -1;
                        }
                        else
                        {
                            // Normal case
                            elementObj["mean"] = iter->second.mean;
                            elementObj["variance"] = iter->second.m2 / iter->second.count;
                            elementObj["count"] = iter->second.count;
                        }

                        jObj[iter->first] = std::move(elementObj);

                        // Reset the stats after this reporting cycle                        
                        iter->second.mean = 0;
                        iter->second.m2 = 0;
                        iter->second.count = 0;
                    }
                }

                jObj["type"] = "elementlatency";
                _broker->PublishAsync("analytics", jObj.dump().c_str());
            }
        });

        return hr;
    }

    ComPtr<IMessageBroker> _broker;

    std::mutex _elementDataMapMutex;
    std::unordered_map<std::string, ElementData> _elementDataMap;

    std::thread _reportingThread;
    int32_t _accumulationTime = 0;
    bool _shutdown = false;
    AutoResetEvent _sampleEvt;
};

G_BEGIN_DECLS
typedef struct _GstElementLatency GstElementLatency;
typedef struct _GstElementLatencyClass GstElementLatencyClass;

GType gst_element_latency_tracer_get_type(void);
#define GST_TYPE_ELEMENT_LATENCY_TRACER (gst_element_latency_tracer_get_type())
#define GST_ELEMENT_LATENCY_TRACER_CAST(obj) ((GstElementLatency *)(obj))

struct _GstElementLatency {
    GstTracer parent;
    ElementLatency* element_latency = nullptr;
};

struct _GstElementLatencyClass {
    GstTracerClass parent_class;
};

G_DEFINE_TYPE(GstElementLatency, gst_element_latency_tracer, GST_TYPE_TRACER);
G_END_DECLS

std::string getName(GstElement* element)
{
    if (element)
    {
        gchar * elementName = gst_element_get_name(element);
        if (elementName)
        {
            std::string name = elementName;
            g_free(elementName);
            return name;
        }
        else
        {
            // Can g_free a nullptr
            g_free(elementName);
            throw std::runtime_error("Element has no name!");
        }
    }
    else
    {
        throw std::runtime_error("Element is nullptr!");
    }
}

std::string getName(GstPad* pad)
{
    if (pad)
    {
        GstElement* element = gst_pad_get_parent_element(pad);
        if (element)
        {
            try
            {
                std::string ret = getName(element);
                gst_object_unref(element);
                return ret;
            }
            catch (const std::exception &e)
            {
                gst_object_unref(element);
                throw e;    
            }
        }
        else
        {
            throw std::runtime_error("Pad has no parent element!");
        }
    }
    else
    {
        throw std::runtime_error("Pad is nullptr!");
    }
}

void push_callback(GstTracer* self, GstClockTime ts, GstPad* pad)
{
    if (!self || !pad)
    {
        return;
    }

    GstElementLatency* element_latency_tracer = GST_ELEMENT_LATENCY_TRACER_CAST(self);

    // get the name of this pad
    std::string name;
    try
    {
        name = getName(pad);
    }
    catch(const std::exception& e)
    {
        TraceWarning(e.what());
        return;
    }
    
    // the next pad that this event corresponds to
    GstPad* nextPad = gst_pad_get_peer(pad);
    std::string nextName;
    try
    {
        nextName = getName(nextPad);
    }
    catch(const std::exception& e)
    {
        TraceWarning(e.what());
        if (nextPad)
        {
            gst_object_unref(nextPad);
        }
        return;
    }
    gst_object_unref(nextPad);

    // set the start time on the next element. calculate the diff on the current element.
    element_latency_tracer->element_latency->CalculateLatency(name, nextName, ts);
}

static void
element_new(GObject* self, GstClockTime ts, GstElement* element)
{
    if (!self || !element)
    {
        return;
    }

    GstElementLatency* element_latency_tracer = GST_ELEMENT_LATENCY_TRACER_CAST(self);

    // Our latency calculation is done with hooks that are called when buffers move from one pad to another.
    // If an element has multiple sink or source pads, then our calculations will be off as we cannot tell which entry event corresponds to which exit event.
    if (element->numsinkpads == 1 && element->numsrcpads == 1)
    {
        try
        {
            element_latency_tracer->element_latency->RegisterElement(getName(element));
        }
        catch (const std::exception &e)
        {
            TraceWarning(e.what());
            return;
        }
    }
}

static void
element_change_state_post(GObject* self, GstClockTime ts, GstElement* element, GstStateChange transition, GstStateChangeReturn result)
{
    if (!self || !element)
    {
        return;
    }

    if (transition == GST_STATE_CHANGE_READY_TO_NULL)
    {
        GstElementLatency* element_latency_tracer = GST_ELEMENT_LATENCY_TRACER_CAST(self);
        try
        {
            element_latency_tracer->element_latency->DeregisterElement(getName(element));
        }
        catch (const std::exception &e)
        {
            TraceWarning(e.what());
        }
    }
}

static void
pad_push_pre(GstTracer* self, GstClockTime ts, GstPad* pad, GstBuffer* buffer)
{
    push_callback(self, ts, pad);
}

static void
pad_push_list_pre(GstTracer* self, GstClockTime ts, GstPad* pad, GstBufferList* list)
{
    // Assumption: If it enters the plugin as a list, it exits the plugin as a list. Basically treating it as if it processed a single buffer.
    // Otherwise, our calculations will be a bit off, but it won't crash.
    push_callback(self, ts, pad);
}

static void
gst_element_latency_tracer_finalize(GObject* obj)
{
    if (obj)
    {
        GstElementLatency* self = GST_ELEMENT_LATENCY_TRACER_CAST(obj);
        if(self->element_latency != nullptr)
        {
            self->element_latency->Release();
        }
    }
    G_OBJECT_CLASS(gst_element_latency_tracer_parent_class)->finalize(obj);
}

static void
gst_element_latency_tracer_class_init(GstElementLatencyClass* klass)
{
    if (klass)
    {
        GObjectClass *gobject_class = G_OBJECT_CLASS(klass);
        gobject_class->finalize = gst_element_latency_tracer_finalize;
    }
}

static void
gst_element_latency_tracer_init (GstElementLatency* self)
{
    if (!self)
    {
        TraceError("Unable to create ElementLatency tracer: nullptr during init");
        return;
    }
    GstTracer* tracer = GST_TRACER(self);

    HRESULT hr = ElementLatency::Create(&(self->element_latency), 1000);
    TraceInfo("Initializing ElementLatency tracer");
    if(FAILED(hr))
    {
        TraceError("Unable to create ElementLatency tracer: %s", ErrorCodeToString(hr));
    }
    else
    {
        gst_tracing_register_hook (tracer, "element-new", G_CALLBACK (element_new));
        gst_tracing_register_hook (tracer, "element-change-state-post", G_CALLBACK (element_change_state_post));
        gst_tracing_register_hook (tracer, "pad-push-pre", G_CALLBACK (pad_push_pre));
        gst_tracing_register_hook (tracer, "pad-push-list-pre", G_CALLBACK (pad_push_list_pre));
    }
}

static gboolean plugin_init(GstPlugin* plugin) 
{
    gst_tracer_register(plugin, "elementlatency", gst_element_latency_tracer_get_type());
    return TRUE;
}

#ifndef VERSION
#define VERSION "1.0.0"
#endif
#ifndef PACKAGE
#define PACKAGE "elementlatency"
#endif
#ifndef PACKAGE_NAME
#define PACKAGE_NAME "elementlatency"
#endif
#ifndef GST_PACKAGE_ORIGIN
#define GST_PACKAGE_ORIGIN "aws"
#endif

GST_PLUGIN_DEFINE(GST_VERSION_MAJOR, GST_VERSION_MINOR, 
    elementlatency, "Tracer to compute element latencies",
    plugin_init, VERSION, "Proprietary", PACKAGE_NAME, GST_PACKAGE_ORIGIN)
