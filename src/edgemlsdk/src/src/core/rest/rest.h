#ifndef __RESTAPI_H__
#define __RESTAPI_H__

#include <Panorama/comobj.h>
#include <Panorama/buffer.h>

namespace Panorama
{
    enum class MethodType
    {
        GET,
        POST
    };

    inline const char* MethodTypeString(MethodType type)
    {
        switch (type)
        {
        case Panorama::MethodType::GET:
            return "GET";
        case Panorama::MethodType::POST:
            return "POST";
        default:
            return "";
        }
    }

    enum class HttpStatusCode
    {
        Unknown = 0,
        OK = 200,
        BadRequest = 400,
        UnsupportedMeditaType = 415,
        BadGateway = 502
    };

    DEF_INTERFACE(IHttpRequest, "{3b689319-efd0-487e-a325-75d6386e15b4}", IUnknownAlias)
    {
        virtual HRESULT SetPath(const char* path) = 0;
        virtual const char* GetPath() const = 0;

        virtual HRESULT AddHeader(const char* key, const char* value) = 0;
        virtual void IterateHeaders(void (*cb)(const char* key, const char* value, void* pUserData), void* pUserData) = 0;

        virtual HRESULT AddBody(IBuffer* body) = 0;
        virtual HRESULT GetBody(IBuffer** ppObj) = 0;

        virtual void SetMethod(MethodType methodType) = 0;
        virtual MethodType GetMethod() const = 0;
        virtual const char* ToString() const = 0;
    };

    DEF_INTERFACE(IHttpResponse, "{c063fdb0-3d5b-4444-b180-948c6561f67a}", IUnknownAlias)
    {
        virtual HttpStatusCode GetStatusCode() const = 0;
        virtual HRESULT GetBody(IBuffer** ppObj) const = 0;
        virtual const char* ToString() const = 0;
    };

    DEF_INTERFACE(IRestAPI, "{083542cc-0855-4665-9226-28c022ba21b4}", IUnknownAlias)
    {
        virtual HRESULT MakeRequest(IHttpResponse** ppObj, IHttpRequest* request) = 0;
    };

    DLLAPI HRESULT CreateRESTClient(IRestAPI** ppObj, const char* ip, int port = 0, bool silent=false);
    DLLAPI HRESULT CreateHttpRequest(IHttpRequest** ppObj);
    DLLAPI HRESULT CreateHttpResponse(IHttpResponse** ppObj, HttpStatusCode statusCode, IBuffer* body = nullptr);
}

#endif