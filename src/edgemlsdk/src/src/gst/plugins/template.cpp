#include <mutex>

#include <gst/gst.h>
#include <gst/base/gstbasetransform.h>

#include <Panorama/message_broker.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/eventing.h>
#include <Panorama/gst.h>
#include <app/device_env.h>
#include <misc.h>
#include <scheduling.h>
#include <queue>

using namespace Panorama;

// ================ GStreamer Boiler Plate Below ======================
G_BEGIN_DECLS

// Ctrl+H 'emltemplate' to 'eml<your-plugin>'
// Visual Code should respect case
#define GST_TYPE_EMLTEMPLATE   (gst_emltemplate_get_type())
#define GST_EMLTEMPLATE(obj)   (G_TYPE_CHECK_INSTANCE_CAST((obj),GST_TYPE_EMLTEMPLATE,GstEmltemplate))
#define GST_EMLTEMPLATE_CLASS(klass)   (G_TYPE_CHECK_CLASS_CAST((klass),GST_TYPE_EMLTEMPLATE,GstEmltemplateClass))
#define GST_IS_EMLTEMPLATE(obj)   (G_TYPE_CHECK_INSTANCE_TYPE((obj),GST_TYPE_EMLTEMPLATE))
#define GST_IS_EMLTEMPLATE_CLASS(obj)   (G_TYPE_CHECK_CLASS_TYPE((klass),GST_TYPE_EMLTEMPLATE))
#define PLUGIN_DESCRIPTION "Your plugin description"
#define PLUGIN_LONG_NAME "Long Name"
#define PLUGIN_CLASSIFICATION "Transform/Video"
#define PLUGIN_AUTHOR "EdgeML-SDK <todo: alias>"

#ifndef VERSION
#define VERSION "1.0.0"
#endif
#ifndef PACKAGE
#define PACKAGE "emltemplate"
#endif
#ifndef PACKAGE_NAME
#define PACKAGE_NAME "emltemplate"
#endif
#ifndef GST_PACKAGE_ORIGIN
#define GST_PACKAGE_ORIGIN "https://alpha.www.docs.aws.a2z.com/edgeml-sdk/v1/1.0/index.html"
#endif

typedef struct _GstEmltemplate GstEmltemplate;
typedef struct _GstEmltemplateClass GstEmltemplateClass;
struct _GstEmltemplate
{
    GstBaseTransform base_emltemplate;
};

struct _GstEmltemplateClass
{
    GstBaseTransformClass base_emltemplate_class;
};

static GstElementClass *parent_class = NULL;
GType gst_emltemplate_get_type(void);

G_END_DECLS

GST_DEBUG_CATEGORY_STATIC(gst_emltemplate_debug_category);
#define GST_CAT_DEFAULT gst_emltemplate_debug_category

gboolean emltemplate_init(GstPlugin* emltemplate)
{
    return gst_element_register(emltemplate, "emltemplate", GST_RANK_NONE, GST_TYPE_EMLTEMPLATE);
}

GST_PLUGIN_DEFINE(GST_VERSION_MAJOR,
    GST_VERSION_MINOR,
    emltemplate,
    PLUGIN_DESCRIPTION,
    emltemplate_init, VERSION, "Proprietary", PACKAGE_NAME, GST_PACKAGE_ORIGIN)

typedef enum
{
    PROP_0
} PluginProperty;

void gst_emltemplate_set_property(GObject* object, guint property_id, const GValue* value, GParamSpec* pspec)
{
    GstEmltemplate* emltemplate = GST_EMLTEMPLATE(object);
}

void gst_emltemplate_get_property(GObject* object, guint property_id, GValue* value, GParamSpec* pspec)
{
    GstEmltemplate* emltemplate = GST_EMLTEMPLATE(object);
}

GstFlowReturn gst_emltemplate_chain(GstPad* pad, GstObject* parent, GstBuffer* buf)
{
    GstEmltemplate* emltemplate = GST_EMLTEMPLATE(parent);
    return gst_pad_push(emltemplate->base_emltemplate.srcpad, buf);
}

static GstStateChangeReturn gst_emltemplate_change_state(GstElement *element, GstStateChange transition) 
{
    // Set the interval epoch when transitioning to the playing state
    GstEmltemplate* capture = GST_EMLTEMPLATE(element);

    // Call the parent class's change_state method
    return GST_ELEMENT_CLASS(parent_class)->change_state(element, transition);
}

static GstStaticPadTemplate gst_emltemplate_src_template =
GST_STATIC_PAD_TEMPLATE("src",
    GST_PAD_SRC,
    GST_PAD_ALWAYS,
    GST_STATIC_CAPS("ANY")
);

static GstStaticPadTemplate gst_emltemplate_sink_template =
GST_STATIC_PAD_TEMPLATE("sink",
    GST_PAD_SINK,
    GST_PAD_ALWAYS,
    GST_STATIC_CAPS("ANY")
);

/* class initialization */
G_DEFINE_TYPE_WITH_CODE(GstEmltemplate, gst_emltemplate, GST_TYPE_BASE_TRANSFORM,
    GST_DEBUG_CATEGORY_INIT(gst_emltemplate_debug_category, "emltemplate", 0,
        "debug category for emltemplate element"));

void gst_emltemplate_dispose(GObject* object)
{
    GstEmltemplate* emltemplate = GST_EMLTEMPLATE(object);
    GST_DEBUG_OBJECT(emltemplate, "dispose");

    /* clean up as possible.  may be called multiple times */
    G_OBJECT_CLASS(gst_emltemplate_parent_class)->dispose(object);
}

void gst_emltemplate_finalize(GObject* object)
{
    GstEmltemplate* emltemplate = GST_EMLTEMPLATE(object);

    GST_DEBUG_OBJECT(emltemplate, "finalize");
    G_OBJECT_CLASS(gst_emltemplate_parent_class)->finalize(object);
}

void gst_emltemplate_class_init(GstEmltemplateClass* klass)
{
    GObjectClass* gobject_class = (GObjectClass*)klass;
    GstElementClass* gstelement_class = (GstElementClass*)klass;

    gst_element_class_set_details_simple(gstelement_class,
        PLUGIN_LONG_NAME,
        PLUGIN_CLASSIFICATION,
        PLUGIN_DESCRIPTION, 
        PLUGIN_AUTHOR);

    gst_element_class_add_static_pad_template(gstelement_class, &gst_emltemplate_sink_template);
    gst_element_class_add_static_pad_template(gstelement_class, &gst_emltemplate_src_template);

    gobject_class->finalize = gst_emltemplate_finalize;
    gstelement_class->change_state = gst_emltemplate_change_state;
    parent_class = static_cast<GstElementClass*>(g_type_class_peek_parent(klass));

    /* Properties */
    gobject_class->set_property = gst_emltemplate_set_property;
    gobject_class->get_property = gst_emltemplate_get_property;
}

void gst_emltemplate_init(GstEmltemplate* emltemplate)
{
    gst_pad_set_chain_function(emltemplate->base_emltemplate.sinkpad, gst_emltemplate_chain);
}
