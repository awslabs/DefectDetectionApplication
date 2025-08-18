#include <mutex>
#include <queue>
#include <optional>

#include <gst/gst.h>
#include <gst/base/gstbasetransform.h>

#include <Panorama/message_broker.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/eventing.h>
#include <Panorama/gst.h>
#include <mlops/triton/triton.h>
#include <misc.h>
#include <scheduling.h>

using namespace Panorama;


class GstTriton
{
public:
    HRESULT Initialize()
    {
        HRESULT hr = S_OK;
        CHECKNULL_OR_EMPTY(_modelRepo.c_str(), E_INVALID_STATE);
        CHECKNULL_OR_EMPTY(_serverPath.c_str(), E_INVALID_STATE);
        CHECKNULL_OR_EMPTY(_model.c_str(), E_INVALID_STATE);

        CHECKHR(CreateTritonInferenceServer(_server.AddressOf(), _modelRepo.c_str(), _serverPath.c_str(), _unique));
        CHECKHR(_server->LoadModel(_model.c_str()));
        CHECKHR(CheckModelLoaded());
        CHECKHR(CreateTritonRequest(_request.AddressOf(), _server, _model.c_str()));
        if(_metadata.size() > 0)
        {
            int32_t metadata_idx = _request->GetInputTensorIndex("METADATA");
            CHECKIF_MSG(metadata_idx == -1, E_INVALIDARG, "Attempted to set metadata on a model that doesn't have an input layer named 'METADATA'");

            ComPtr<ITensor> metadata_tensor;
            CHECKHR(_request->Input(metadata_tensor.AddressOf(), metadata_idx));

            if(metadata_tensor->Abstract() == false)
            {
                // The tensor isn't abstract, 
                // Ensure the provided metadata matches the size of the metadata tensor buffer
                // Then copy metadata to that tensor
                ComPtr<IBuffer> metadata_buffer;
                CHECKHR(metadata_tensor->Buffer(metadata_buffer.AddressOf()));
                CHECKIF_MSG(_metadata.length() != metadata_buffer->Size(), E_OUTOFRANGE, "Metadata is not the same size as the metadata tensor");
                memcpy(metadata_buffer->Data(), _metadata.c_str(), _metadata.length());
            }
            else
            {
                // Metadata tensor is abstract, create a concrete tensor with the metadata string, and set as input
                ComPtr<IBuffer> metadata_buffer;
                CHECKHR(Buffer::CreateFromString(metadata_buffer.AddressOf(), _metadata.c_str()));

                ComPtr<ITensor> concrete_tensor;
                CHECKHR(MLOps::Tensor(concrete_tensor.AddressOf(), "METADATA", std::vector<int64_t>{static_cast<int64_t>(metadata_buffer->Size())}, metadata_tensor->DataType(), metadata_buffer));
                CHECKHR(_request->SetInput(concrete_tensor, metadata_idx));
            }
        }

        return hr;
    }

    void SetModelRepo(const char* value)
    {
        _modelRepo = value;
    }

    const char* GetModelRepo()
    {
        return _modelRepo.c_str();
    }

    void SetServerPath(const char* value)
    {
        _serverPath = value;
    }

    const char* GetServerPath()
    {
        return _serverPath.c_str();
    }

    void SetModel(const char* value)
    {
        _model = value;
    }

    const char* GetModel()
    {
        return _model.c_str();
    }

    void SetUnique(bool value)
    {
        _unique = value;
    }

    bool GetUnique()
    {
        return _unique;
    }

    void SetMetadata(const char* value)
    {
        _metadata = value;
    }

    const char* GetMetadata()
    {
        return _metadata.c_str();
    }

    ComPtr<IInferenceRequest> GetRequest()
    {
        return _request;
    }

    void SetCorrelationId(const char* value)
    {
        _correlationId = value;
    }

    const char* GetCorrelationId()
    {
        return _correlationId.c_str();
    }

    HRESULT SetInput(GstBuffer* buffer)
    {
        HRESULT hr = S_OK;

        GstMapInfo map;
        gst_buffer_map(buffer, &map, GST_MAP_READ);

        hr = [&]() -> HRESULT
        {
            ComPtr<ITensor> input_tensor;
            CHECKHR(_request->Input(input_tensor.AddressOf(), 0));
            // Abstract, dynamic tensor here.
            if(input_tensor->Abstract()){
                // Some checks
                int64_t buffer_width = GetBufferWidth();
                int64_t buffer_height = GetBufferHeight();
                CHECKIF_MSG(buffer_width <= 0 ,E_OUTOFRANGE, "gst buffer width should be > 0");
                CHECKIF_MSG(buffer_height <= 0,E_OUTOFRANGE, "gst buffer height should be > 0");
                ComPtr<IBuffer> data;
                // For now this is safe to assume.
                int64_t num_of_channels = 3;
                uint64_t total = buffer_width * buffer_height * num_of_channels;
                CHECKHR(Buffer::Create(data.AddressOf(),total));

                // remove line padding if there is any.
                if(map.size != total){
                    memset(data->Data(), 0, total);
                    int64_t width_in_bytes = map.size / buffer_height;
                    char* data_ptr = (char*)data->Data();
                    for (int r = 0; r < buffer_height; r++) {
                        memcpy(&data_ptr[r * buffer_width * 3], &map.data[r * width_in_bytes], buffer_width * 3);
                    }
                }
                else{
                    memcpy(data->Data(), map.data, map.size);
                }
                ComPtr<ITensor> new_tensor;

                // Use existing name and data type, from abstract definition.
                MLOps::Tensor(new_tensor.AddressOf(), input_tensor->Name(), { buffer_height, buffer_width, num_of_channels }, input_tensor->DataType(), data);
                _request->SetInput(new_tensor, 0);
                TraceVerbose("Done setting dynamic tensor");
            }
            else {
                // Concrete tensor, copy data from buffer
                ComPtr<IBuffer> input_buffer;
                CHECKHR(input_tensor->Buffer(input_buffer.AddressOf()));
                CHECKIF_MSG(map.size != input_buffer->Size(), E_OUTOFRANGE, "Incoming GstBuffer is not the same size as the input tensor");
                memcpy(input_buffer->Data(), map.data, map.size);
            }
            return S_OK;
        }();

        gst_buffer_unmap(buffer, &map);
        return hr;
    }

    HRESULT Process()
    {
        return _server->ProcessRequest(_request);
    }

    HRESULT CheckModelLoaded()
    {
        std::string status = std::string(_server->GetModelStatus(_model.c_str()));
        CHECKIF_MSG(status != "READY", E_INVALID_STATE, "Model is not ready for inference, wait till it is in READY state and check if model name is valid." );
        return S_OK;
    }

    uint32_t GetNumOfOutputs()
    {
        return _request->GetNumOfOutputTensors();
    }

    HRESULT GetResult(ITensor** ppObj, uint32_t& index)
    {
        return _request->MoveOutput(ppObj, index);
    }

    void SetBufferWidth(const int64_t& value)
    {
        _buffer_width = value;
    }

    void SetBufferHeight(const int64_t& value)
    {
        _buffer_height = value;
    }

    int64_t GetBufferWidth()
    {
        return _buffer_width;
    }

    int64_t GetBufferHeight()
    {
        return _buffer_height;
    }
private:
    std::string _modelRepo, _serverPath, _model, _metadata, _correlationId;
    bool _unique = false;
    int64_t _buffer_width, _buffer_height;
    ComPtr<IInferenceServer> _server;
    ComPtr<IInferenceRequest> _request;
};

// ================ GStreamer Boiler Plate Below ======================
G_BEGIN_DECLS

// Ctrl+H 'emltemplate' to 'eml<your-plugin>'
// Visual Code should respect case
#define GST_TYPE_emltriton   (gst_emltriton_get_type())
#define GST_emltriton(obj)   (G_TYPE_CHECK_INSTANCE_CAST((obj),GST_TYPE_emltriton,Gstemltriton))
#define GST_emltriton_CLASS(klass)   (G_TYPE_CHECK_CLASS_CAST((klass),GST_TYPE_emltriton,GstemltritonClass))
#define GST_IS_emltriton(obj)   (G_TYPE_CHECK_INSTANCE_TYPE((obj),GST_TYPE_emltriton))
#define GST_IS_emltriton_CLASS(obj)   (G_TYPE_CHECK_CLASS_TYPE((klass),GST_TYPE_emltriton))
#define PLUGIN_DESCRIPTION "Wrapper around NVIDIDA Triton Inference Server"
#define PLUGIN_LONG_NAME "EdgeML-SDK Triton Inference Server"
#define PLUGIN_CLASSIFICATION "Transform/Video"
#define PLUGIN_AUTHOR "EdgeML-SDK <todo: alias>"

#ifndef VERSION
#define VERSION "1.0.0"
#endif
#ifndef PACKAGE
#define PACKAGE "emltriton"
#endif
#ifndef PACKAGE_NAME
#define PACKAGE_NAME "emltriton"
#endif
#ifndef GST_PACKAGE_ORIGIN
#define GST_PACKAGE_ORIGIN "https://alpha.www.docs.aws.a2z.com/edgeml-sdk/v1/1.0/index.html"
#endif

typedef struct _Gstemltriton Gstemltriton;
typedef struct _GstemltritonClass GstemltritonClass;
struct _Gstemltriton
{
    GstBaseTransform base_emltriton;
    GstTriton* triton;
};

struct _GstemltritonClass
{
    GstBaseTransformClass base_emltriton_class;
};

static GstElementClass *parent_class = NULL;
GType gst_emltriton_get_type(void);

G_END_DECLS

GST_DEBUG_CATEGORY_STATIC(gst_emltriton_debug_category);
#define GST_CAT_DEFAULT gst_emltriton_debug_category

gboolean emltriton_init(GstPlugin* emltriton)
{
    return gst_element_register(emltriton, "emltriton", GST_RANK_NONE, GST_TYPE_emltriton);
}

GST_PLUGIN_DEFINE(GST_VERSION_MAJOR,
    GST_VERSION_MINOR,
    emltriton,
    PLUGIN_DESCRIPTION,
    emltriton_init, VERSION, "Proprietary", PACKAGE_NAME, GST_PACKAGE_ORIGIN)

typedef enum
{
    PROP_0,
    MODEL_REPO,
    SERVER_PATH,
    MODEL,
    UNIQUE,
    METADATA,
    CORRELATION_ID
} PluginProperty;

void gst_emltriton_set_property(GObject* object, guint property_id, const GValue* value, GParamSpec* pspec)
{
    Gstemltriton* emltriton = GST_emltriton(object);
    switch (property_id) 
    {
        case PluginProperty::MODEL_REPO:
            emltriton->triton->SetModelRepo(g_value_get_string(value));
            break;
        case PluginProperty::SERVER_PATH:
            emltriton->triton->SetServerPath(g_value_get_string(value));
            break;
        case PluginProperty::MODEL:
            emltriton->triton->SetModel(g_value_get_string(value));
            break;
        case PluginProperty::UNIQUE:
            emltriton->triton->SetUnique(g_value_get_boolean(value));
            break;
        case PluginProperty::METADATA:
            emltriton->triton->SetMetadata(g_value_get_string(value));
            break;
        case PluginProperty::CORRELATION_ID:
            emltriton->triton->SetCorrelationId(g_value_get_string(value));
            break;
    }
}

void gst_emltriton_get_property(GObject* object, guint property_id, GValue* value, GParamSpec* pspec)
{
    Gstemltriton* emltriton = GST_emltriton(object);

    switch (property_id) 
    {
        case PluginProperty::MODEL_REPO:
            g_value_set_string(value, emltriton->triton->GetModelRepo());
            break;
        case PluginProperty::SERVER_PATH:
            g_value_set_string(value, emltriton->triton->GetServerPath());
            break;
        case PluginProperty::MODEL:
            g_value_set_string(value, emltriton->triton->GetModel());
            break;
        case PluginProperty::UNIQUE:
            g_value_set_boolean(value, emltriton->triton->GetUnique());
            break;
        case PluginProperty::METADATA:
            g_value_set_string(value, emltriton->triton->GetMetadata());
            break;
        case PluginProperty::CORRELATION_ID:
            g_value_set_string(value, emltriton->triton->GetCorrelationId());
            break;
    }
}

static HRESULT
gst_emltriton_set_inference_result_tags(GstBaseTransform* transform,
                                      bool anomalous, float confidence_value) {
    Gstemltriton* emltriton = GST_emltriton(transform);
    GstTagList* taglist;
    gchar* tagstring;
    gboolean is_anomalous = anomalous;
    gfloat confidence = confidence_value;

    tagstring = g_strdup_printf("taglist,is_anomalous=%d,confidence=%g", is_anomalous, confidence);

    taglist = gst_tag_list_new_from_string(tagstring);
    g_free(tagstring);
    CHECKIF_MSG(!taglist, E_FAIL, "Error creating taglist");
    CHECKIF_MSG(gst_tag_list_is_empty(taglist), E_FAIL, "Taglist is empty");
    CHECKIF_MSG(!gst_pad_push_event(GST_BASE_TRANSFORM_SRC_PAD(transform),
                                    gst_event_new_tag(taglist)), E_FAIL, "Error pushing taglist event");
    return S_OK;
}

GstFlowReturn gst_emltriton_chain(GstPad* pad, GstObject* parent, GstBuffer* buf)
{
    HRESULT hr = S_OK;
    Gstemltriton* emltriton = GST_emltriton(parent);
    // Check ModelStatus
    CHECK_FAIL(emltriton->triton->CheckModelLoaded(), GST_FLOW_CUSTOM_ERROR);
    CHECK_FAIL(emltriton->triton->SetInput(buf), GST_FLOW_CUSTOM_ERROR);
    CHECK_FAIL(emltriton->triton->Process(), GST_FLOW_CUSTOM_ERROR);
    uint32_t num_of_outputs = emltriton->triton->GetNumOfOutputs();
    std::optional<bool> anomalous;
    std::optional<float> confidence;
    for (uint32_t output_idx = 0; output_idx < num_of_outputs; ++output_idx)
    {
        ComPtr<ITensor> output_tensor;
        CHECK_FAIL(emltriton->triton->GetResult(output_tensor.AddressOf(), output_idx), GST_FLOW_CUSTOM_ERROR);
        std::string output_name = "triton_inference_" + std::string(output_tensor->Name());
        ComPtr<IBuffer> output_buffer;
        CHECK_FAIL(output_tensor->Buffer(output_buffer.AddressOf()), GST_FLOW_CUSTOM_ERROR);
        // Outputs like mask and overlays may be empty. no need to write them to emlcapture.
        if (output_buffer != nullptr) {
            if(std::string(output_tensor->Name()) == "output_anomalous") {
                anomalous = *output_buffer->Data();
                TraceInfo("Anomalous: %d", anomalous.value());
            }
            if(std::string(output_tensor->Name()) == "output_confidence") {
                confidence = *(float*)output_buffer->Data();
                TraceInfo("Confidence: %f", confidence.value());
            }
            ComPtr<IPayload> payload;
            CHECK_FAIL(MessageBroker::CreatePayload(payload.AddressOf(), output_buffer), GST_FLOW_CUSTOM_ERROR);
            if(strlen(emltriton->triton->GetCorrelationId()) > 0)
            {
                CHECK_FAIL(SetBufferCorrelationId(buf, emltriton->triton->GetCorrelationId()), GST_FLOW_ERROR);
                CHECK_FAIL(payload->SetCorrelationId(emltriton->triton->GetCorrelationId()), GST_FLOW_ERROR);
            }
            CHECK_FAIL(GStreamer::AddPayloadToBuffer(payload, buf, output_name.c_str()), GST_FLOW_CUSTOM_ERROR);
        }
    }
    if (anomalous.has_value() && confidence.has_value()) {
        CHECK_FAIL(gst_emltriton_set_inference_result_tags(GST_BASE_TRANSFORM(emltriton), anomalous.value(), confidence.value()), GST_FLOW_CUSTOM_ERROR);
    }
    return gst_pad_push(emltriton->base_emltriton.srcpad, buf);
}

static GstStateChangeReturn gst_emltriton_change_state(GstElement *element, GstStateChange transition) 
{
    // Set the interval epoch when transitioning to the playing state
    Gstemltriton* capture = GST_emltriton(element);

    switch(transition)
    {
        case GST_STATE_CHANGE_NULL_TO_READY:
            if(FAILED(capture->triton->Initialize()))
            {
                TraceError("Failed to initialize underlying triton server");
                return GST_STATE_CHANGE_FAILURE;
            }

            break;
        default:
            break;
    }

    // Call the parent class's change_state method
    return GST_ELEMENT_CLASS(parent_class)->change_state(element, transition);
}

static GstStaticPadTemplate gst_emltriton_src_template =
GST_STATIC_PAD_TEMPLATE("src",
    GST_PAD_SRC,
    GST_PAD_ALWAYS,
    GST_STATIC_CAPS("ANY")
);

static GstStaticPadTemplate gst_emltriton_sink_template =
GST_STATIC_PAD_TEMPLATE("sink",
    GST_PAD_SINK,
    GST_PAD_ALWAYS,
    GST_STATIC_CAPS("ANY")
);

/* class initialization */
G_DEFINE_TYPE_WITH_CODE(Gstemltriton, gst_emltriton, GST_TYPE_BASE_TRANSFORM,
    GST_DEBUG_CATEGORY_INIT(gst_emltriton_debug_category, "emltriton", 0,
        "debug category for emltriton element"));

void gst_emltriton_dispose(GObject* object)
{
    Gstemltriton* emltriton = GST_emltriton(object);
    GST_DEBUG_OBJECT(emltriton, "dispose");

    /* clean up as possible.  may be called multiple times */
    G_OBJECT_CLASS(gst_emltriton_parent_class)->dispose(object);
}

void gst_emltriton_finalize(GObject* object)
{
    Gstemltriton* emltriton = GST_emltriton(object);

    if(emltriton->triton != nullptr)
    {
        delete emltriton->triton;
        emltriton->triton = nullptr;
    }

    GST_DEBUG_OBJECT(emltriton, "finalize");
    G_OBJECT_CLASS(gst_emltriton_parent_class)->finalize(object);
}

void gst_emltriton_class_init(GstemltritonClass* klass)
{
    GObjectClass* gobject_class = (GObjectClass*)klass;
    GstElementClass* gstelement_class = (GstElementClass*)klass;

    gst_element_class_set_details_simple(gstelement_class,
        PLUGIN_LONG_NAME,
        PLUGIN_CLASSIFICATION,
        PLUGIN_DESCRIPTION, 
        PLUGIN_AUTHOR);

    gst_element_class_add_static_pad_template(gstelement_class, &gst_emltriton_sink_template);
    gst_element_class_add_static_pad_template(gstelement_class, &gst_emltriton_src_template);

    gobject_class->finalize = gst_emltriton_finalize;
    gstelement_class->change_state = gst_emltriton_change_state;
    parent_class = static_cast<GstElementClass*>(g_type_class_peek_parent(klass));

    /* Properties */
    gobject_class->set_property = gst_emltriton_set_property;
    gobject_class->get_property = gst_emltriton_get_property;

    g_object_class_install_property(gobject_class, PluginProperty::MODEL_REPO,
        g_param_spec_string("model-repo", "Model Repository", "[REQUIRED] Path to the directory containing your models",
                         "", static_cast<GParamFlags>(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));

    g_object_class_install_property(gobject_class, PluginProperty::SERVER_PATH,
        g_param_spec_string("server-path", "Triton Server Path", "[REQUIRED] Path to the installation of the your triton server",
                         "", static_cast<GParamFlags>(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));

    g_object_class_install_property(gobject_class, PluginProperty::MODEL,
        g_param_spec_string("model", "The Model", "Name of the model to load",
                         "", static_cast<GParamFlags>(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));

    g_object_class_install_property(gobject_class, PluginProperty::UNIQUE,
        g_param_spec_boolean("unique", "Unique Server", "Flag indicating to create a new server instance, default is false.  If set to false should call MLOps::ReleaseTritonServers to gracefully shutdown servers",
                         false, static_cast<GParamFlags>(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));

    g_object_class_install_property(gobject_class, PluginProperty::METADATA,
        g_param_spec_string("metadata", "Metadata", "User provided metadata as a string",
                         "", static_cast<GParamFlags>(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));

    g_object_class_install_property(gobject_class, PluginProperty::CORRELATION_ID,
        g_param_spec_string("correlation-id", "Correlation Id", "Correlation ID attached to the GST Buffer",
                         "", static_cast<GParamFlags>(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));
}

// pad probe for width and height.
static GstPadProbeReturn pad_probe(GstPad* pad, GstPadProbeInfo* info, gpointer user_data) {
    GstEvent* event = GST_PAD_PROBE_INFO_EVENT(info);
    if (GST_EVENT_CAPS == GST_EVENT_TYPE(event)) {
        GstCaps* caps = gst_caps_new_any();
        int width, height;
        gst_event_parse_caps(event, &caps);
        GstStructure* s = gst_caps_get_structure(caps, 0);
        if (!gst_structure_get_int(s, "width", &width) ||
            !gst_structure_get_int(s, "height", &height)) {
            g_print("no dimensions\n");
            return GST_PAD_PROBE_REMOVE;
        }
        Gstemltriton* emltriton = (Gstemltriton*)user_data;
        emltriton->triton->SetBufferWidth(width);
        emltriton->triton->SetBufferHeight(height);
        GST_DEBUG("DO PROBE, height %d, width %d\n", height, width);
    }
    return GST_PAD_PROBE_OK;
}

void gst_emltriton_init(Gstemltriton* emltriton)
{
    gst_pad_use_fixed_caps(emltriton->base_emltriton.sinkpad);
    emltriton->triton = new GstTriton();
    gst_pad_set_chain_function(emltriton->base_emltriton.sinkpad, gst_emltriton_chain);
    gst_pad_add_probe(emltriton->base_emltriton.sinkpad, GST_PAD_PROBE_TYPE_EVENT_BOTH, pad_probe,
                      (gpointer)emltriton, nullptr);
}
