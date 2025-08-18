#include <sstream>

#include <Panorama/comptr.h>
#include <Panorama/flowcontrol.h>

#include "rest.h"

using namespace Panorama;

class HttpResponse : public UnknownImpl<IHttpResponse>
{
public:
    static HRESULT Create(IHttpResponse** ppObj, HttpStatusCode statusCode, IBuffer* body)
    {
        HRESULT hr = S_OK;
        CHECKNULL(ppObj, E_POINTER);
        *ppObj = nullptr;

        ComPtr<HttpResponse> ptr;
        ptr.Attach(new (std::nothrow) HttpResponse());
        CHECKNULL(ptr, E_OUTOFMEMORY);

        ptr->_status = statusCode;
        ptr->_body = body;

        *ppObj = ptr.Detach();
        return hr;
    }

    HttpStatusCode GetStatusCode() const override
    {
        return _status;
    }

    HRESULT GetBody(IBuffer** ppObj) const override
    {
        HRESULT hr = S_OK;
        CHECKNULL(ppObj, E_POINTER);

        *ppObj = _body.Detach();
        return hr;
    }

    const char* ToString() const override
    {
        std::stringstream msg;
        msg << "Status: " << static_cast<int>(_status) << "\n";

        if (_body)
        {
            msg << "Body: " << _body->AsString() << "\n";
        }

        _toString = msg.str();
        return _toString.c_str();
    }

private:
    HttpResponse() = default;
    HttpStatusCode _status = HttpStatusCode::Unknown;
    mutable ComPtr<IBuffer> _body;
    mutable std::string _toString;
};

DLLAPI HRESULT Panorama::CreateHttpResponse(IHttpResponse** ppObj, HttpStatusCode statusCode, IBuffer* body)
{
    return HttpResponse::Create(ppObj, statusCode, body);
}