#include <mutex>
#include <unordered_map>

#include <gst/gst.h>
#include <gst/base/gstbasetransform.h>

#include <nlohmann/json.hpp>
#include <Panorama/app.h>
#include <Panorama/message_broker.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/eventing.h>
#include <Panorama/gst.h>
#include <misc.h>

#include <exif-data.h>
#include <jpeglib.h>
using namespace Panorama;

// ================ GStreamer Boiler Plate Below ======================
G_BEGIN_DECLS

#define GST_TYPE_EMEXIFEXTRACT (gst_emexifextract_get_type())
#define GST_EMEXIFEXTRACT(obj) (G_TYPE_CHECK_INSTANCE_CAST((obj), GST_TYPE_EMEXIFEXTRACT, GstEmExifExtract))
#define GST_EMEXIFEXTRACT_CLASS(klass) (G_TYPE_CHECK_CLASS_CAST((klass), GST_TYPE_EMEXIFEXTRACT, GstEmExifExtractClass))
#define GST_IS_EMEXIFEXTRACT(obj) (G_TYPE_CHECK_INSTANCE_TYPE((obj), GST_TYPE_EMEXIFEXTRACT))
#define GST_IS_EMEXIFEXTRACT_CLASS(klass) (G_TYPE_CHECK_CLASS_TYPE((klass), GST_TYPE_EMEXIFEXTRACT))
#define PLUGIN_DESCRIPTION "Plugin that extracts EXIF from frame."
typedef struct _GstEmExifExtract GstEmExifExtract;
typedef struct _GstEmExifExtractClass GstEmExifExtractClass;
 
struct _GstEmExifExtract {
    GstBaseTransform parent;
 };
struct _GstEmExifExtractClass {
    GstBaseTransformClass parent_class;
};
 
GType gst_emexifextract_get_type(void);
 
gboolean emexifextract_plugin_init(GstPlugin* plugin);
 
G_END_DECLS

#define EmExifExtract_plugin_name "emexifextract"

GST_DEBUG_CATEGORY_STATIC(gst_emexifextract_debug_category);
#define GST_CAT_DEFAULT gst_emexifextract_debug_category

gboolean emexifextract_init(GstPlugin* emexifextract)
{
    return gst_element_register(emexifextract, "emexifextract", GST_RANK_NONE, GST_TYPE_EMEXIFEXTRACT);
}

#ifndef VERSION
#define VERSION "1.0.0"
#endif
#ifndef PACKAGE
#define PACKAGE "emexifextract"
#endif
#ifndef PACKAGE_NAME
#define PACKAGE_NAME "emexifextract"
#endif
#ifndef GST_PACKAGE_ORIGIN
#define GST_PACKAGE_ORIGIN "aws"
#endif

GST_PLUGIN_DEFINE(GST_VERSION_MAJOR,
    GST_VERSION_MINOR,
    emexifextract,
    PLUGIN_DESCRIPTION,
    emexifextract_init, VERSION, "LGPL", PACKAGE_NAME, GST_PACKAGE_ORIGIN)

static void gst_emexifextract_dispose(GObject* object);
static void gst_emexifextract_finalize(GObject* object);
static GstFlowReturn gst_emexifextract_set_image_orientation_tag(GstBaseTransform* transform,
                                                                 guint exif_orientation);
static GstFlowReturn gst_emexifextract_transform_ip(GstBaseTransform* transform, GstBuffer* buf);
static void gst_emexifextract_class_init(GstEmExifExtractClass* klass);
static void gst_emexifextract_init(GstEmExifExtract* emexifextract);
 
 
static GstStaticPadTemplate gst_emexifextract_src_template =
    GST_STATIC_PAD_TEMPLATE("src", GST_PAD_SRC, GST_PAD_ALWAYS, GST_STATIC_CAPS("image/jpeg"));
 
static GstStaticPadTemplate gst_emexifextract_sink_template =
    GST_STATIC_PAD_TEMPLATE("sink", GST_PAD_SINK, GST_PAD_ALWAYS, GST_STATIC_CAPS("image/jpeg"));
 

 
const std::unordered_map<int, gchar*> exif_image_orientations = {
    {1, (gchar*)"rotate-0"},       {2, (gchar*)"flip-rotate-0"}, {3, (gchar*)"rotate-180"},      {4, (gchar*)"flip-rotate-180"},
    {5, (gchar*)"flip-rotate-90"}, {6, (gchar*)"rotate-90"},     {7, (gchar*)"flip-rotate-270"}, {8, (gchar*)"rotate-270"},
};
 
#define gst_emexifextract_parent_class parent_class
G_DEFINE_TYPE_WITH_CODE(GstEmExifExtract, gst_emexifextract, GST_TYPE_BASE_TRANSFORM,
                        GST_DEBUG_CATEGORY_INIT(gst_emexifextract_debug_category, "emexifextract", 0,
                                                "debug category for emexifextract element"));
 
static void gst_emexifextract_class_init(GstEmExifExtractClass* klass) {
    GObjectClass* gobject_class = G_OBJECT_CLASS(klass);
    GstElementClass* gstelement_class = GST_ELEMENT_CLASS(klass);
    GstBaseTransformClass* gstbasetransform_class = GST_BASE_TRANSFORM_CLASS(klass);
 
    gobject_class->dispose = gst_emexifextract_dispose;
    gobject_class->finalize = gst_emexifextract_finalize;
 
    gstbasetransform_class->transform_ip = gst_emexifextract_transform_ip;
 
    gst_element_class_set_details_simple(
        gstelement_class, PACKAGE_NAME, "extract exif information ",
        "EM EXIF Extract GStreamer Plugin for edgeml-sdk",
        "aws");
 
    gst_element_class_add_static_pad_template(gstelement_class, &gst_emexifextract_src_template);
    gst_element_class_add_static_pad_template(gstelement_class, &gst_emexifextract_sink_template);
 
    gst_element_class_set_static_metadata(
        gstelement_class, PACKAGE_NAME,
        "EM EXIF Extract GStreamer Plugin for edgeml-sdk", "EM EXIF Extract",
        "aws");
}
 
static void gst_emexifextract_init(GstEmExifExtract* emexifextract) {
    gst_pad_use_fixed_caps(emexifextract->parent.sinkpad);
 
    std::chrono::milliseconds time_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now().time_since_epoch());
    TraceInfo("emexifextract_logger_%s",std::to_string(time_ms.count()).c_str());
}
 
static GstFlowReturn gst_emexifextract_set_image_orientation_tag(GstBaseTransform* transform,
                                                                 guint exif_orientation) {
    GstEmExifExtract* emexifextract = GST_EMEXIFEXTRACT(transform);
    GstTagList* taglist;
    gchar* image_orientation;
    gchar* tagstring;
 
    if (exif_image_orientations.find(exif_orientation) == exif_image_orientations.end()) {
        TraceError("EXIF orientation found in image metadata is not valid: %s. Defaulting to 1 (rotate-0).", 
                                     std::to_string(exif_orientation).c_str());
        exif_orientation = 1;
    }
 
    image_orientation = exif_image_orientations.at(exif_orientation);
    TraceInfo("Setting image-orientation tag with EXIF orientation: %s(%s).",
                                std::to_string(exif_orientation).c_str(), image_orientation);
    tagstring = g_strdup_printf("taglist,image-orientation=%s", image_orientation);
 
    taglist = gst_tag_list_new_from_string(tagstring);
    g_free(tagstring);
    if (!taglist) {
        TraceError("Encountered error while parsing taglist.");
        return GST_FLOW_ERROR;
    }
 
    if (!gst_tag_list_is_empty(taglist)) {
        if (!gst_pad_push_event(GST_BASE_TRANSFORM_SRC_PAD(transform),
                                gst_event_new_tag(taglist))) {
            TraceError("Unable to push new tag event.");
            return GST_FLOW_ERROR;
        }
    } else {
        TraceError("Taglist is empty, no tags will be set.");
        return GST_FLOW_ERROR;
    }
 
    return GST_FLOW_OK;
}
 
static GstFlowReturn gst_emexifextract_transform_ip(GstBaseTransform* transform, GstBuffer* buf) {
    GstEmExifExtract* emexifextract = GST_EMEXIFEXTRACT(transform);
    guint size;
    GstMapInfo map;
    ExifData* exif_data;
    ExifEntry* orientation_entry;
    ExifShort exif_orientation;
 
    if (!gst_buffer_map(buf, &map, GST_MAP_READ)) {
        TraceError("Encountered error during read in gst_buffer_map().");
        return GST_FLOW_ERROR;
    }
 
    size = map.size;
    exif_data = exif_data_new_from_data(map.data, size);
    gst_buffer_unmap(buf, &map);
    if (!exif_data) {
        TraceError("Encountered unknown error while retrieving EXIF data.");
        return GST_FLOW_ERROR;
    }
 
    orientation_entry = exif_data_get_entry(exif_data, EXIF_TAG_ORIENTATION);
    if (!orientation_entry) {
        TraceError(
            "EXIF orientation entry not found. Defaulting to 1 (rotate-0).");
        exif_orientation = 1;
    } else {
        exif_orientation =
            exif_get_short(orientation_entry->data, exif_data_get_byte_order(exif_data));
    }
 
    exif_data_unref(exif_data);
 
    return gst_emexifextract_set_image_orientation_tag(transform, exif_orientation);
}
 
static void gst_emexifextract_dispose(GObject* object) {
    GstEmExifExtract* emexifextract = GST_EMEXIFEXTRACT(object);
 
    G_OBJECT_CLASS(gst_emexifextract_parent_class)->dispose(object);
}
 
static void gst_emexifextract_finalize(GObject* object) {
    GstEmExifExtract* emexifextract = GST_EMEXIFEXTRACT(object);
 
    G_OBJECT_CLASS(gst_emexifextract_parent_class)->finalize(object);
}
