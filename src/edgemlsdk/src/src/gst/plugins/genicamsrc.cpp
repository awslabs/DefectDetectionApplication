/*
 * Copyright 2025 Amazon Web Services, Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
#include <thread>
#include <gst/gst.h>
#include <gst/base/gstpushsrc.h>
#include <arv.h>
#include <stdio.h>
#include <Panorama/app.h>
#include <Panorama/message_broker.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/eventing.h>
#include <app/device_env.h>
#include <nlohmann/json.hpp>
#include <misc.h>
#include <Panorama/gst.h>
#include <mutex>

using namespace Panorama;

G_BEGIN_DECLS

#define GST_TYPE_GENICAM_SRC (gst_genicam_src_get_type())
#define GST_GENICAM_SRC(obj) (G_TYPE_CHECK_INSTANCE_CAST((obj), GST_TYPE_GENICAM_SRC, GstGenicamSrc))
#define GST_GENICAM_SRC_CLASS(klass) (G_TYPE_CHECK_CLASS_CAST((klass), GST_TYPE_GENICAM_SRC, GstGenicamSrcClass))
#define GST_IS_GENICAM_SRC(obj) (G_TYPE_CHECK_INSTANCE_TYPE((obj), GST_TYPE_GENICAM_SRC))
#define GST_IS_GENICAM_SRC_CLASS(obj) (G_TYPE_CHECK_CLASS_TYPE((klass), GST_TYPE_GENICAM_SRC))

typedef struct _GstGenicamSrc GstGenicamSrc;
typedef struct _GstGenicamSrcClass GstGenicamSrcClass;
#define CORRELATION_ID "CORRELATION_ID"
#define BUFFER_TIMEOUT_MICRO_SECONDS 100000

struct _GstGenicamSrc
{
    GstPushSrc parent;
    std::string camera_name;
    std::string features;
    double gain;
    double exposure_time_us;
    ArvCamera *camera;
    gint offset_x;
    gint offset_y;
    GstCaps *all_caps;
    int32_t frame_token;
    ComPtr<IMessageBroker> broker;
    std::condition_variable frame_trigger;
    gboolean eos;
    gboolean frame_request_received; // set to true when frame trigger request is received
    std::string correlation_id;
    std::mutex mtx;                             // mutex is used to lock frame_trigger, eos, frame_request_received, correlation_id
    std::thread stop_acq_thread;                // thread performs arv_camera_stop_acquisition
    gboolean async_stop_acquisition_successful; // set to false when arv_camera_stop_acquisition fails
};

struct _GstGenicamSrcClass
{
    GstPushSrcClass parent_class;
};

G_END_DECLS

GST_DEBUG_CATEGORY_STATIC(genicam_src_debug);
#define GST_CAT_DEFAULT genicam_src_debug

// ================= GStreamer ======================//

enum
{
    PROP_0,
    PROP_CAMERA_NAME,
    PROP_CAMERA,
    PROP_GAIN,
    PROP_EXPOSURE,
    PROP_OFFSET_X,
    PROP_OFFSET_Y,
    PROP_FEATURES
};

static GstStaticPadTemplate gst_genicam_src_template =
    GST_STATIC_PAD_TEMPLATE("src",
                            GST_PAD_SRC,
                            GST_PAD_ALWAYS,
                            GST_STATIC_CAPS("ANY"));

#define parent_class gst_genicam_src_parent_class
G_DEFINE_TYPE_WITH_CODE(GstGenicamSrc, gst_genicam_src, GST_TYPE_PUSH_SRC,
                        GST_DEBUG_CATEGORY_INIT(genicam_src_debug, "genicamsrc", 0,
                                                "debug category for genicamsrc element"));

GType gst_genicam_src_get_type(void);

static void gst_genicam_src_init(GstGenicamSrc *gst_genicam_src);
static void gst_genicam_src_class_init(GstGenicamSrcClass *klass);
static void gst_genicam_src_set_property(GObject *object, guint prop_id, const GValue *value, GParamSpec *pspec);
static void gst_genicam_src_get_property(GObject *object, guint prop_id, GValue *value, GParamSpec *pspec);
static gboolean gst_genicam_src_set_caps(GstBaseSrc *src, GstCaps *caps);
static GstCaps *gst_genicam_src_get_caps(GstBaseSrc *src, GstCaps *filter);
static gboolean gst_genicam_src_start(GstBaseSrc *src);
static gboolean gst_genicam_src_stop(GstBaseSrc *src);
static GstFlowReturn gst_genicam_src_create(GstPushSrc *push_src, GstBuffer **buffer);
static void gst_genicam_src_finalize(GObject *object);

HRESULT Initialize(GstGenicamSrc *gst_genicam_src)
{
    HRESULT hr = S_OK;
    ComPtr<ICredentialProvider> credential_provider;
    CHECKHR(CreatePlatformCredentialProvider(credential_provider.AddressOf()));
    CHECKHR(MessageBroker::Create(gst_genicam_src->broker.AddressOf(), credential_provider));
    CHECKHR(gst_genicam_src->broker->Initialize());
    return hr;
}

HRESULT SubscribeToReleaseFrame(GstGenicamSrc *gst_genicam_src)
{
    HRESULT hr = S_OK;
    hr = gst_genicam_src->broker->Subscribe("release-frame", [gst_genicam_src](IPayload *payload)
                                            {
                                                std::string command = payload->SerializeAsString();
                                                TraceInfo("GenicamSrc received command %s", command.c_str());
                                                if (nlohmann::json::accept(command) == false)
                                                {
                                                    GST_WARNING_OBJECT(gst_genicam_src, "Command sent to genicamsrc plugin was not valid JSON");
                                                    return;
                                                }
                                                nlohmann::json json = nlohmann::json::parse(command);
                                                if(ValidateJsonProperty<const char*>(json, CORRELATION_ID, true) == false) {
                                                    GST_DEBUG_OBJECT(gst_genicam_src, "No CORRELATION_ID in payload schema");
                                                }
                                                {
                                                    std::unique_lock<std::mutex> lk(gst_genicam_src->mtx);
                                                    if (json.contains(CORRELATION_ID)) 
                                                    {
                                                        gst_genicam_src->correlation_id = json[CORRELATION_ID];
                                                    }
                                                    else
                                                    {
                                                        GST_DEBUG_OBJECT(gst_genicam_src, "Setting Correlation ID to empty - %s", gst_genicam_src->correlation_id.c_str());
                                                        if(!gst_genicam_src->correlation_id.empty())
                                                        {
                                                            gst_genicam_src->correlation_id.clear();
                                                        }
                                                    }
                                                    gst_genicam_src->frame_request_received = true;
                                                }
                                                gst_genicam_src->frame_trigger.notify_all();
                                            });
    if (FAILED(hr))
    {
        GST_ERROR_OBJECT(gst_genicam_src, "Failed to subscribe to release-frame command : %s", ErrorCodeToString(hr));
    }
    gst_genicam_src->frame_token = hr;
    return hr;
}

static GstCaps *gst_genicam_src_get_all_camera_caps(GstGenicamSrc *gst_genicam_src, GError **error)
{
    GError *local_error = NULL;
    GstCaps *caps;
    gint64 *pixel_formats = NULL;
    int min_height, min_width;
    int max_height, max_width;
    unsigned int n_pixel_formats, i;

    GST_DEBUG_OBJECT(gst_genicam_src, "Get all camera caps");
    // check if gst_genicam_src is valid otherwise return
    g_return_val_if_fail(GST_IS_GENICAM_SRC(gst_genicam_src), NULL);

    if (!ARV_IS_CAMERA(gst_genicam_src->camera))
    {
        return NULL;
    }

    // get width bounds
    arv_camera_get_width_bounds(gst_genicam_src->camera, &min_width, &max_width, &local_error);
    // get height bounds
    if (!local_error)
        arv_camera_get_height_bounds(gst_genicam_src->camera, &min_height, &max_height, &local_error);

    // get number of supported pixel formats
    if (!local_error)
        pixel_formats = arv_camera_dup_available_pixel_formats(gst_genicam_src->camera, &n_pixel_formats,
                                                               &local_error);
    if (local_error)
    {
        g_propagate_error(error, local_error);
        return NULL;
    }

    caps = gst_caps_new_empty();

    for (i = 0; i < n_pixel_formats; i++)
    {
        const char *caps_string;

        caps_string = arv_pixel_format_to_gst_caps_string(pixel_formats[i]);

        if (caps_string != NULL)
        {
            GstStructure *structure;

            structure = gst_structure_from_string(caps_string, NULL);
            gst_structure_set(structure,
                              "width", GST_TYPE_INT_RANGE, min_width, max_width,
                              "height", GST_TYPE_INT_RANGE, min_height, max_height,
                              NULL);
            gst_caps_append_structure(caps, structure);
        }
    }

    g_free(pixel_formats);
    return caps;
}
static gboolean gst_genicam_src_init_camera(GstGenicamSrc *gst_genicam_src, GError **error)
{
    GError *local_error = NULL;
    if (gst_genicam_src->camera != NULL)
        g_object_unref(gst_genicam_src->camera);
    gst_genicam_src->camera = arv_camera_new(gst_genicam_src->camera_name.c_str(), &local_error);
    if (!local_error)
        arv_camera_get_region(gst_genicam_src->camera, &gst_genicam_src->offset_x, &gst_genicam_src->offset_y, NULL,
                              NULL, &local_error);

    if (local_error)
    {
        g_clear_object(&gst_genicam_src->camera);
        g_propagate_error(error, local_error);
        return FALSE;
    }
    return TRUE;
}
static void gst_genicam_src_camera_error(GstGenicamSrc *gst_genicam_src, GError *error)
{
    if (error->domain == ARV_DEVICE_ERROR && error->code == ARV_DEVICE_ERROR_NOT_FOUND)
    {
        GST_ELEMENT_ERROR(gst_genicam_src, RESOURCE, NOT_FOUND,
                          ("Could not find camera \"%s\": %s",
                           gst_genicam_src->camera_name.c_str(),
                           error->message),
                          (NULL));
    }
    else
    {
        GST_ELEMENT_ERROR(gst_genicam_src, RESOURCE, READ,
                          ("Could not read camera \"%s\": %s",
                           gst_genicam_src->camera_name.c_str(),
                           error->message),
                          (NULL));
    }

    g_error_free(error);
}

static void gst_genicam_src_finalize(GObject *object)
{
    GstGenicamSrc *gst_genicam_src = GST_GENICAM_SRC(object);

    GST_DEBUG_OBJECT(gst_genicam_src, "finalize");
    gst_genicam_src->broker.Release();
    gst_genicam_src->broker.Detach();
    GST_DEBUG_OBJECT(gst_genicam_src, "finalize Complete");
    /* clean up object here */
    G_OBJECT_CLASS(parent_class)->finalize(object);
}

static GstStateChangeReturn gst_genicam_src_change_state(GstElement *element, GstStateChange transition)
{
    GstGenicamSrc *gst_genicam_src = GST_GENICAM_SRC(element);
    if (transition == GST_STATE_CHANGE_PAUSED_TO_READY)
    {
        if (gst_genicam_src->frame_token != -1)
        {
            HRESULT hr = gst_genicam_src->broker->Unsubscribe(gst_genicam_src->frame_token);
            if (FAILED(hr))
            {
                GST_WARNING_OBJECT(gst_genicam_src, "Failed to Unsubscribe to release frame command");
            }
        }
        {
            std::unique_lock<std::mutex> lk(gst_genicam_src->mtx);
            gst_genicam_src->eos = true;
        }
        gst_genicam_src->frame_trigger.notify_all();
    }
    else if (transition == GST_STATE_CHANGE_READY_TO_PAUSED)
    {
        SubscribeToReleaseFrame(gst_genicam_src);
        gst_genicam_src->eos = false;
    }
    return GST_ELEMENT_CLASS(parent_class)->change_state(element, transition);
}
static GstFlowReturn gst_genicam_src_create(GstPushSrc *push_src, GstBuffer **buffer)
{
    // Expect gst_genicam_src_create as a sequential call by gstreamer framework, i.e. only one gst_genicam_src_create execution can happen at any point in time
    GstGenicamSrc *gst_genicam_src = GST_GENICAM_SRC(push_src);
    char *buffer_data;
    size_t buffer_size;
    ArvBuffer *arv_buffer = nullptr;
    GError *error = NULL;
    bool is_eos = false;
    std::string correlation_id;
    ArvStream *stream = nullptr;
    gint payload;

    if (gst_genicam_src->stop_acq_thread.joinable())
    {
        gst_genicam_src->stop_acq_thread.join();
        if (gst_genicam_src->async_stop_acquisition_successful == false)
        {
            return GST_FLOW_ERROR;
        }
    }

    GST_DEBUG_OBJECT(gst_genicam_src, "Waiting for Frame Trigger");
    {
        std::unique_lock<std::mutex> lk(gst_genicam_src->mtx);
        gst_genicam_src->frame_trigger.wait(lk, [&]
                                            { return gst_genicam_src->frame_request_received || gst_genicam_src->eos; });
        is_eos = gst_genicam_src->eos;
        correlation_id = gst_genicam_src->correlation_id;
    }
    if (is_eos == false)
    {
        GST_DEBUG_OBJECT(gst_genicam_src, "Start Acquisition");

        payload = arv_camera_get_payload(gst_genicam_src->camera, &error);
        if (error)
        {
            GST_ERROR_OBJECT(gst_genicam_src, "Failed to get camera Payload : %s - ", error->message);
            return GST_FLOW_ERROR;
        }
        stream = arv_camera_create_stream(gst_genicam_src->camera, NULL, NULL, &error);
        if (error)
        {
            GST_ERROR_OBJECT(gst_genicam_src, "Failed to create camera stream : %s - ", error->message);
            return GST_FLOW_ERROR;
        }
        arv_stream_push_buffer(stream, arv_buffer_new(payload, NULL)); // thread safe
        arv_camera_start_acquisition(gst_genicam_src->camera, &error);
        if (!error)
        {
            arv_buffer = arv_stream_timeout_pop_buffer(stream, BUFFER_TIMEOUT_MICRO_SECONDS); // thread safe
            g_object_unref(stream);
            if (ARV_IS_BUFFER(arv_buffer) && arv_buffer_get_status(arv_buffer) == ARV_BUFFER_STATUS_SUCCESS)
            {
                GST_DEBUG_OBJECT(gst_genicam_src, "Acquisition Successful");
            }
            else
            {
                GST_ERROR_OBJECT(gst_genicam_src, "Invalid Aravis Buffer, Acquisition Failed");
                arv_camera_stop_acquisition(gst_genicam_src->camera, &error);
                if (error)
                {
                    GST_ERROR_OBJECT(gst_genicam_src, "Failed to Stop Camera Acquisition : %s - ", error->message); // log failure on stop acquisition
                }
                return GST_FLOW_ERROR;
            }
        }
        else
        {
            GST_ERROR_OBJECT(gst_genicam_src, "Acquisition Failed, error %s - ", error->message);
            g_object_unref(stream);
            return GST_FLOW_ERROR;
        }
        gst_genicam_src->stop_acq_thread = std::thread([gst_genicam_src]
                                                       {
            GError* error = NULL;
            arv_camera_stop_acquisition(gst_genicam_src->camera, &error);
            if (error) 
            {
                GST_ERROR_OBJECT(gst_genicam_src, "Failed to Stop Camera Acquisition : %s - ", error->message); // log failure on stop acquisition
                gst_genicam_src->async_stop_acquisition_successful = false;
            } });

        // keeping it in line with python implementation of https://code.amazon.com/packages/EdgeMLDefectDetectionLocalServer-Backend/blobs/2905156e6d1f07993e4be5e4bc0a865eb23e2c64/--/src/utils/camera_manager.py#L70
        buffer_data = (char *)(arv_buffer_get_data(arv_buffer, &buffer_size));
        *buffer = gst_buffer_new_wrapped(buffer_data, buffer_size);
        if (!correlation_id.empty())
        {
            HRESULT hr = SetBufferCorrelationId(*buffer, correlation_id.c_str());
            if (FAILED(hr))
            {
                GST_ERROR_OBJECT(gst_genicam_src, "Failed to set buffer correlation id with error : %s", ErrorCodeToString(hr));
                if (gst_genicam_src->stop_acq_thread.joinable())
                {
                    gst_genicam_src->stop_acq_thread.join();
                }
                return GST_FLOW_ERROR;
            }
        }
        {
            std::unique_lock<std::mutex> lk(gst_genicam_src->mtx);
            gst_genicam_src->frame_request_received = false;
        }
        return GST_FLOW_OK;
    }
    else
    {
        return GST_FLOW_EOS;
    }
}

static gboolean gst_genicam_src_stop(GstBaseSrc *src)
{
    GstGenicamSrc *gst_genicam_src = GST_GENICAM_SRC(src);
    GError *error = NULL;

    GST_DEBUG_OBJECT(gst_genicam_src, "Close Camera %s", gst_genicam_src->camera_name.c_str());
    if (gst_genicam_src->camera != nullptr)
        g_object_unref(gst_genicam_src->camera);

    if (gst_genicam_src->all_caps != NULL)
        gst_caps_unref(gst_genicam_src->all_caps);

    if (error)
    {
        GST_ERROR_OBJECT(gst_genicam_src, "Acquisition stop error : %s", error->message);
        g_error_free(error);
        return FALSE;
    }

    return TRUE;
}

static gboolean gst_genicam_src_start(GstBaseSrc *src)
{
    GstGenicamSrc *gst_genicam_src = GST_GENICAM_SRC(src);
    GError *error = NULL;
    gboolean result = FALSE;

    GST_DEBUG_OBJECT(gst_genicam_src, "Open Camera %s", gst_genicam_src->camera_name.c_str());

    result = gst_genicam_src_init_camera(gst_genicam_src, &error);

    if (result)
        gst_genicam_src->all_caps = gst_genicam_src_get_all_camera_caps(gst_genicam_src, &error);

    if (error)
        gst_genicam_src_camera_error(gst_genicam_src, error);
    return result;
}

static GstCaps *gst_genicam_src_get_caps(GstBaseSrc *src, GstCaps *filter)
{
    GstGenicamSrc *gst_genicam_src = GST_GENICAM_SRC(src);
    GstCaps *caps;
    GstCaps *filtered_caps;

    if (gst_genicam_src->all_caps != NULL)
    {
        caps = gst_caps_copy(gst_genicam_src->all_caps);
        GST_DEBUG_OBJECT(gst_genicam_src, "Available caps camera caps = %" GST_PTR_FORMAT, caps);
    }
    else
    {
        caps = gst_caps_new_any(); // return any caps for gstreamer to call start and open camera, get all_caps
        GST_DEBUG_OBJECT(gst_genicam_src, "Available caps any caps = %" GST_PTR_FORMAT, caps);
        return caps;
    }
    if (filter != NULL)
    {
        filtered_caps = gst_caps_intersect(caps, filter);
        gst_caps_unref(caps);
        GST_DEBUG_OBJECT(gst_genicam_src, "Available caps filtered caps = %" GST_PTR_FORMAT, filtered_caps);
        return filtered_caps;
    }

    GST_DEBUG_OBJECT(gst_genicam_src, "Available caps unfiltered caps = %" GST_PTR_FORMAT, caps);
    return caps;
}

static gboolean gst_genicam_src_set_caps(GstBaseSrc *src, GstCaps *caps)
{
    GError *error = NULL;
    GstGenicamSrc *gst_genicam_src = GST_GENICAM_SRC(src);
    GstStructure *structure = NULL;
    ArvPixelFormat pixel_format;
    gint height;
    gint width;
    gint current_height;
    gint current_width;
    int depth = 0;
    int bpp = 0;
    gboolean result = FALSE;
    gboolean is_gain_available;
    gboolean is_exposure_time_available;
    const char *format_string;

    GST_DEBUG_OBJECT(gst_genicam_src, "Requested caps = %" GST_PTR_FORMAT, caps);
    structure = gst_caps_get_structure(caps, 0);

    // get current width and height supported by camera
    arv_camera_get_region(gst_genicam_src->camera, NULL, NULL, &current_width, &current_height, &error);
    if (error)
    {
        GST_ERROR_OBJECT(gst_genicam_src, "Failed to get current width and height from camera %s ", gst_genicam_src->camera_name.c_str());
        g_error_free(error);
        return result;
    }

    // check if gain and exposure time values are available for current camera
    is_gain_available = arv_camera_is_gain_available(gst_genicam_src->camera, NULL);
    is_exposure_time_available = arv_camera_is_exposure_time_available(gst_genicam_src->camera, NULL);

    // get width, height, depth, bpp, format_string from received negotiable caps structure
    gst_structure_get_int(structure, "width", &width);
    gst_structure_get_int(structure, "height", &height);
    gst_structure_get_int(structure, "depth", &depth);
    gst_structure_get_int(structure, "bpp", &bpp);
    format_string = gst_structure_get_string(structure, "format");

    // get aravis pixel format from gst caps structure
    pixel_format = arv_pixel_format_from_gst_caps(gst_structure_get_name(structure), format_string, bpp, depth);

    if (!pixel_format)
    {
        GST_ERROR_OBJECT(src, "did not find matching pixel_format");
    }

    // set negotiated pixel format
    if (!error)
        arv_camera_set_pixel_format(gst_genicam_src->camera, pixel_format, &error);

    if (!error)
    {
        if (width != current_width || height != current_height)
            arv_camera_set_region(gst_genicam_src->camera,
                                  gst_genicam_src->offset_x, gst_genicam_src->offset_y,
                                  width, height, &error);
    }

    if (!error)
    {
        if (is_gain_available)
            arv_camera_set_gain(gst_genicam_src->camera, gst_genicam_src->gain, &error);
        if (is_exposure_time_available && !error)
            arv_camera_set_exposure_time(gst_genicam_src->camera, gst_genicam_src->exposure_time_us, &error);
    }
    if (!error)
        arv_device_set_features_from_string(arv_camera_get_device(gst_genicam_src->camera), gst_genicam_src->features.c_str(), &error);

    result = TRUE;
    if (error)
    {
        GST_ELEMENT_ERROR(gst_genicam_src, RESOURCE, WRITE, ("Could not set caps on camera \"%s\": %s", gst_genicam_src->camera_name.c_str(), error->message), (NULL));
        result = FALSE;
        g_error_free(error);
    }

    GST_DEBUG_OBJECT(gst_genicam_src, "Requested caps are set on source = %s", result ? "TRUE" : "FALSE");
    return result;
}

static void gst_genicam_src_get_property(GObject *object, guint prop_id, GValue *value, GParamSpec *pspec)
{
    GstGenicamSrc *gst_genicam_src = GST_GENICAM_SRC(object);

    GST_DEBUG_OBJECT(gst_genicam_src, "getting property %s", pspec->name);

    switch (prop_id)
    {
    case PROP_CAMERA_NAME:
        g_value_set_string(value, gst_genicam_src->camera_name.c_str());
        break;
    case PROP_CAMERA:
        g_value_set_object(value, gst_genicam_src->camera);
        break;
    case PROP_GAIN:
        g_value_set_double(value, gst_genicam_src->gain);
        break;
    case PROP_EXPOSURE:
        g_value_set_double(value, gst_genicam_src->exposure_time_us);
        break;
    case PROP_OFFSET_X:
        g_value_set_int(value, gst_genicam_src->offset_x);
        break;
    case PROP_OFFSET_Y:
        g_value_set_int(value, gst_genicam_src->offset_y);
        break;
    case PROP_FEATURES:
        g_value_set_string(value, gst_genicam_src->features.c_str());
        break;
    default:
        G_OBJECT_WARN_INVALID_PROPERTY_ID(object, prop_id, pspec);
        break;
    }
}

static void gst_genicam_src_set_property(GObject *object, guint prop_id, const GValue *value, GParamSpec *pspec)
{
    GstGenicamSrc *gst_genicam_src = GST_GENICAM_SRC(object);

    GST_DEBUG_OBJECT(gst_genicam_src, "setting property %s", pspec->name);

    switch (prop_id)
    {
    case PROP_CAMERA_NAME:
        gst_genicam_src->camera_name = std::string(reinterpret_cast<char *>(value->data->v_pointer));
        break;

    case PROP_GAIN:

        gst_genicam_src->gain = g_value_get_double(value);
        if (gst_genicam_src->camera != nullptr && arv_camera_is_gain_available(gst_genicam_src->camera, nullptr))
            arv_camera_set_gain(gst_genicam_src->camera, gst_genicam_src->gain, nullptr);

        break;

    case PROP_EXPOSURE:

        gst_genicam_src->exposure_time_us = g_value_get_double(value);
        if (gst_genicam_src->camera != nullptr && arv_camera_is_exposure_time_available(gst_genicam_src->camera, nullptr))
            arv_camera_set_exposure_time(gst_genicam_src->camera, gst_genicam_src->exposure_time_us, nullptr);

        break;
    case PROP_OFFSET_X:
        gst_genicam_src->offset_x = g_value_get_int(value);
        break;
    case PROP_OFFSET_Y:
        gst_genicam_src->offset_y = g_value_get_int(value);
        break;
    case PROP_FEATURES:

        gst_genicam_src->features = std::string(reinterpret_cast<char *>(value->data->v_pointer));

        if (gst_genicam_src->camera != nullptr && arv_camera_get_device(gst_genicam_src->camera) != nullptr)
            arv_device_set_features_from_string(arv_camera_get_device(gst_genicam_src->camera), gst_genicam_src->features.c_str(), nullptr);

        break;
    default:
        G_OBJECT_WARN_INVALID_PROPERTY_ID(object, prop_id, pspec);
        break;
    }
}

static void gst_genicam_src_class_init(GstGenicamSrcClass *klass)
{
    GObjectClass *gobject_class = (GObjectClass *)klass;
    GstElementClass *gst_element_class = (GstElementClass *)klass;
    GstBaseSrcClass *gst_basesrc_class = GST_BASE_SRC_CLASS(klass);
    GstPushSrcClass *gst_pushsrc_class = GST_PUSH_SRC_CLASS(klass);

    gobject_class->set_property = gst_genicam_src_set_property;
    gobject_class->get_property = gst_genicam_src_get_property;
    gobject_class->finalize = gst_genicam_src_finalize;

    // parent class
    gst_element_class->change_state = gst_genicam_src_change_state;
    g_object_class_install_property(gobject_class,
                                    PROP_CAMERA_NAME,
                                    g_param_spec_string("camera-name",
                                                        "Camera name",
                                                        "Name of the camera",
                                                        NULL,
                                                        static_cast<GParamFlags>(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));
    g_object_class_install_property(gobject_class,
                                    PROP_CAMERA,
                                    g_param_spec_object("camera",
                                                        "Camera Object",
                                                        "Camera instance to retrieve additional information",
                                                        ARV_TYPE_CAMERA,
                                                        static_cast<GParamFlags>(G_PARAM_READABLE | G_PARAM_STATIC_STRINGS)));
    g_object_class_install_property(gobject_class,
                                    PROP_GAIN,
                                    g_param_spec_double("gain",
                                                        "Gain",
                                                        "Gain (dB)",
                                                        -1.0, 500.0, 0.0,
                                                        static_cast<GParamFlags>(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));

    g_object_class_install_property(gobject_class,
                                    PROP_EXPOSURE,
                                    g_param_spec_double("exposure",
                                                        "Exposure",
                                                        "Exposure time (µs)",
                                                        -1, 100000000.0, 500.0,
                                                        static_cast<GParamFlags>(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));

    g_object_class_install_property(gobject_class,
                                    PROP_OFFSET_X,
                                    g_param_spec_int("offset-x",
                                                     "x Offset",
                                                     "Offset in x direction",
                                                     0, G_MAXINT, 0,
                                                     static_cast<GParamFlags>(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));

    g_object_class_install_property(gobject_class,
                                    PROP_OFFSET_Y,
                                    g_param_spec_int("offset-y",
                                                     "y Offset",
                                                     "Offset in y direction",
                                                     0, G_MAXINT, 0,
                                                     static_cast<GParamFlags>(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));

    g_object_class_install_property(gobject_class,
                                    PROP_FEATURES,
                                    g_param_spec_string("features",
                                                        "String of feature values",
                                                        "Additional configuration parameters for ArvDevice as a space separated list of feature assignations",
                                                        NULL,
                                                        static_cast<GParamFlags>(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));

    gst_element_class_set_static_metadata(gst_element_class, "genicamsrc", "Generic/Source", "Source Element to fetch frames from Genicam compatible cameras using aravis sdk", "AWS");

    gst_element_class_add_static_pad_template(gst_element_class, &gst_genicam_src_template);
    gst_basesrc_class->start = gst_genicam_src_start;
    gst_basesrc_class->stop = gst_genicam_src_stop;
    gst_basesrc_class->set_caps = gst_genicam_src_set_caps;
    gst_basesrc_class->get_caps = gst_genicam_src_get_caps;
    gst_pushsrc_class->create = gst_genicam_src_create;
}

static void gst_genicam_src_init(GstGenicamSrc *gst_genicam_src)
{

    gst_genicam_src->gain = -1;
    gst_genicam_src->exposure_time_us = -1;
    gst_genicam_src->camera = nullptr;
    gst_genicam_src->offset_x = 0;
    gst_genicam_src->offset_y = 0;
    gst_genicam_src->all_caps = NULL;
    gst_genicam_src->frame_token = -1;
    gst_genicam_src->frame_request_received = false;
    gst_genicam_src->eos = false;
    gst_genicam_src->async_stop_acquisition_successful = true;
    HRESULT hr = Initialize(gst_genicam_src);
    if (FAILED(hr))
    {
        GST_ERROR_OBJECT(gst_genicam_src, "Failed to initialize Genicamsrc plugin: %s", ErrorCodeToString(hr));
    }
}

static gboolean genicam_src_init(GstPlugin *genicam_src)
{
    return gst_element_register(genicam_src, "genicamsrc", GST_RANK_NONE, GST_TYPE_GENICAM_SRC);
}

#ifndef VERSION
#define VERSION "1.0.0"
#endif
#ifndef PACKAGE
#define PACKAGE "genicamsrc"
#endif
#ifndef PACKAGE_NAME
#define PACKAGE_NAME "genicamsrc"
#endif
#ifndef GST_PACKAGE_ORIGIN
#define GST_PACKAGE_ORIGIN "aws"
#endif

GST_PLUGIN_DEFINE(GST_VERSION_MAJOR,
                  GST_VERSION_MINOR,
                  genicamsrc,
                  "Camera Source Plugin for GeniCam Standard Cameras",
                  genicam_src_init, VERSION, "Proprietary", PACKAGE_NAME, GST_PACKAGE_ORIGIN)
