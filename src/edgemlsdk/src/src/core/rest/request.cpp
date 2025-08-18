#include <map>
#include <sstream>

#include <Panorama/comptr.h>
#include <Panorama/flowcontrol.h>

#include "rest.h"

using namespace Panorama;

class HttpRequest : public UnknownImpl<IHttpRequest>
{
public:
    static HRESULT Create(IHttpRequest** ppObj)
    {
        HRESULT hr = S_OK;
        CHECKNULL(ppObj, E_POINTER);
        *ppObj = nullptr;

        ComPtr<HttpRequest> ptr;
        ptr.Attach(new (std::nothrow) HttpRequest());
        CHECKNULL(ptr, E_OUTOFMEMORY);

        *ppObj = ptr.Detach();
        return hr;
    }

    HRESULT SetPath(const char* path) override
    {
        CHECKNULL_MSG(path, E_INVALIDARG, "path cannot be null");
        _path = path;
        return S_OK;
    }

    const char* GetPath() const override
    {
        return _path.c_str();
    }

    HRESULT AddHeader(const char* key, const char* value) override
    {
        HRESULT hr = S_OK;
        CHECKNULL_MSG(key, E_INVALIDARG, "Key cannot be null");
        CHECKNULL_MSG(value, E_INVALIDARG, "Value cannot be null");

        _headers[key] = value;

        return hr;
    }

    void IterateHeaders(void (*cb)(const char* key, const char* value, void* pUserData), void* pUserData) override
    {
        for (auto it = _headers.begin(); it != _headers.end(); it++)
        {
            cb(it->first.c_str(), it->second.c_str(), pUserData);
        }
    }

    HRESULT AddBody(IBuffer* body) override
    {
        _body = body;
        return S_OK;
    }

    HRESULT GetBody(IBuffer** ppObj) override
    {
        CHECKNULL(ppObj, E_POINTER);
        *ppObj = _body.Detach();
        return S_OK;
    }

    void SetMethod(MethodType methodType) override
    {
        _method = methodType;
    }

    MethodType GetMethod() const override
    {
        return _method;
    }

    const char* ToString() const override
    {
        std::stringstream msg;
        msg << _path << "\n";
        msg << "Method: " << MethodTypeString(_method) << "\n";
        msg << "Headers: " << "\n";
        for (auto it = _headers.begin(); it != _headers.end(); it++)
        {
            msg << "\t" << it->first.c_str() << ":" << it->second.c_str() << "\n";
        }

        if (_body)
        {
            msg << "Body: " << _body->AsString() << "\n";
        }

        _toString = msg.str();
        return _toString.c_str();
    }

private:
    HttpRequest() = default;

    std::string _path;
    MethodType _method = MethodType::GET;
    std::map<std::string, std::string> _headers;
    ComPtr<IBuffer> _body;
    mutable std::string _toString;
};

DLLAPI HRESULT Panorama::CreateHttpRequest(IHttpRequest** ppObj)
{
    return HttpRequest::Create(ppObj);
}