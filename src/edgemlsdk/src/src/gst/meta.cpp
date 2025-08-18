#include <Panorama/gst.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/buffer.h>

using namespace Panorama;

#define PAYLOAD_META_TYPE PayloadMeta_GetType()
#define CORRELATION_ID "correlation-id"

struct PayloadMeta
{
    GstMeta meta;
    IPayload* Payload;
    char* Id;
};

GType PayloadMeta_GetType()
{
    static GType type;
    static const gchar *tags[] = {NULL};

    if (g_once_init_enter(&type))
    {
        GType _type = gst_meta_api_type_register("PayloadMetaAPI", tags);
        g_once_init_leave(&type, _type);
    }

    return type;
}

static gboolean PayloadMeta_Init(GstMeta *meta, gpointer params, GstBuffer *buffer)
{
    if (meta == nullptr)
    {
        return false;
    }

    // nobody else should be calling, but doesn't hurt to check
    if (meta->info->api == PAYLOAD_META_TYPE)
    {
        PayloadMeta *payload_meta = reinterpret_cast<PayloadMeta *>(meta);

        payload_meta->Payload = nullptr;
        payload_meta->Id = nullptr;
    }
    else
    {
        return false;
    }

    return true;
}

static gboolean PayloadMeta_Transform(GstBuffer *new_buf, GstMeta *meta, GstBuffer *current_buf, GQuark type, gpointer data)
{
    HRESULT hr = S_OK;
    PayloadMeta *payload_meta = reinterpret_cast<PayloadMeta *>(meta);
    CHECKIF(payload_meta == nullptr, E_FAIL);
    CHECK_FAIL(GStreamer::AddPayloadToBuffer(payload_meta->Payload, new_buf, payload_meta->Id), false);
    return true;
}

static void PayloadMeta_Free(GstMeta *meta, GstBuffer *buffer)
{
    PayloadMeta* payload_meta = reinterpret_cast<PayloadMeta*>(meta);
    if (payload_meta != nullptr)
    {
        if (payload_meta->Payload != nullptr)
        {
            payload_meta->Payload->Release();
            payload_meta->Payload = nullptr;
        }

        if(payload_meta->Id != nullptr)
        {
            free(payload_meta->Id);
            payload_meta->Id = nullptr;
        }
    }
}

const GstMetaInfo *PayloadMeta_GetInfo()
{
    static const GstMetaInfo *meta_info = NULL;

    if (g_once_init_enter(&meta_info))
    {
        const GstMetaInfo *mi = gst_meta_register(PAYLOAD_META_TYPE, "PayloadMeta", sizeof(PayloadMeta), PayloadMeta_Init, PayloadMeta_Free, PayloadMeta_Transform);
        g_once_init_leave(&meta_info, mi);
    }

    return meta_info;
}

DLLAPI HRESULT AddPayloadToBufferMeta(IPayload *payload, GstBuffer *buf, const char *id)
{
    HRESULT hr = S_OK;

    CHECKNULL(payload, E_INVALIDARG);
    CHECKNULL(buf, E_INVALIDARG);
    CHECKNULL_OR_EMPTY(id, E_INVALIDARG);

    PayloadMeta* meta = reinterpret_cast<PayloadMeta*>(gst_buffer_add_meta(buf, PayloadMeta_GetInfo(), nullptr));
    CHECKNULL(meta, E_FAIL);

    payload->AddRef();
    meta->Payload = payload;
    meta->Id = (char*)malloc(strlen(id) * sizeof(char) + 1);
    CHECKNULL(meta->Id, E_OUTOFMEMORY);
    memcpy(meta->Id, id, strlen(id) * sizeof(char) + 1);

    return S_OK;
}

DLLAPI HRESULT GetPayloadFromBufferMeta(IPayload **ppObj, GstBuffer *buf, const char *id)
{
    CHECKNULL(ppObj, E_POINTER);
    CHECKNULL(buf, E_INVALIDARG);
    CHECKNULL_OR_EMPTY(id, E_INVALIDARG);

    gpointer state = NULL;

    // Since possibly multiple implementations of PayloadMeta API need to iterate through all meta
    // to find the PayloadMeta with the matching id
    do
    {
        GstMeta *meta = gst_buffer_iterate_meta(buf, &state);
        if (meta == nullptr)
        {
            // no more metadata objects in the buffer
            break;
        }

        if (meta->info->api == PAYLOAD_META_TYPE)
        {
            PayloadMeta* payload_meta = reinterpret_cast<PayloadMeta*>(meta);
            if (strcmp(payload_meta->Id, id) == 0)
            {
                CHECKIF_MSG(payload_meta->Payload == nullptr, E_INVALID_STATE, "Getting meta with id %s from buffer, but oddly has a NULL payload", id);
                payload_meta->Payload->AddRef();
                *ppObj = payload_meta->Payload;
                return S_OK;
            }
        }
    } while (true);

    return E_NOT_FOUND;
}

DLLAPI HRESULT SetBufferCorrelationId(GstBuffer* buf, const char* correlation_id)
{
    HRESULT hr = S_OK;
    CHECKNULL(buf, E_INVALIDARG);
    CHECKNULL_OR_EMPTY(correlation_id, E_INVALIDARG);

    ComPtr<IPayload> correlation_payload;
    // The content of this payload is the provided correlation id. The payload itself does not have the correlation id field set.
    CHECKHR(MessageBroker::CreatePayload(correlation_payload.AddressOf(), correlation_id));
    CHECKHR(AddPayloadToBufferMeta(correlation_payload, buf, CORRELATION_ID));
    return hr;
}

DLLAPI HRESULT GetBufferCorrelationId(IBuffer** ppObj, GstBuffer* buf)
{
    HRESULT hr = S_OK;
    CHECKNULL(ppObj, E_POINTER);
    CHECKNULL(buf, E_INVALIDARG);

    ComPtr<IPayload> payload;
    hr = GetPayloadFromBufferMeta(payload.AddressOf(), buf, CORRELATION_ID);
    if(hr == E_NOT_FOUND)
    {
        // Avoid spamming error when correlation-id is not found as it's optional
        return hr;
    }
    CHECKHR(hr);
    CHECKHR(payload->Serialize(ppObj));
    return hr;
}