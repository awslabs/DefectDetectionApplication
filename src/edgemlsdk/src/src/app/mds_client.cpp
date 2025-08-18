#include <mutex>
#include <nlohmann/json.hpp>

#include <Panorama/app.h>
#include <Panorama/flowcontrol.h>
#include <core/rest/rest.h>

// todo: Add version information at compile time
#define USER_AGENT "Panorama-V2-SDK"

using namespace Panorama;

class MDSClient : public UnknownImpl<IMDSClient>
{
public:
    static HRESULT Create(IMDSClient** ppObj, const char* ip, int32_t port, const char* nodeUid)
    {
        HRESULT hr = S_OK;
        CREATE_COM(MDSClient, ptr);
        CHECKHR(ptr->Initialize(ip, port, nodeUid));
        *ppObj = ptr.Detach();
        return hr;
    }

    ~MDSClient()
    {
        COM_DTOR(MDSClient);
    }

    HRESULT AnnounceSelf() override
    {
        HRESULT hr = S_OK;

        ComPtr<IHttpRequest> announceRequest;
        CHECKHR(CreateHttpRequest(announceRequest.AddressOf()));
        announceRequest->SetPath("announceSelf");
        announceRequest->SetMethod(MethodType::POST);
        announceRequest->AddHeader("Content-Type", "application/json");
        announceRequest->AddHeader("User-Agent", USER_AGENT);

        nlohmann::json jObj;
        jObj["nodeId"] = _nodeUid;
        jObj["version"] = "v1";

        ComPtr<IBuffer> body;
        CHECKHR(CreateBufferFromString(body.AddressOf(), jObj.dump().c_str()));
        announceRequest->AddBody(body);

        ComPtr<IHttpResponse> httpResponse;
        CHECKHR(_restApi->MakeRequest(httpResponse.AddressOf(), announceRequest));

        ComPtr<IBuffer> buffer;
        httpResponse->GetBody(buffer.AddressOf());
        TraceVerbose("Response from announce self: %s", buffer->AsString());
        if (httpResponse->GetStatusCode() != HttpStatusCode::OK)
        {
            TraceError("AnnounceSelf failed with error code %d", static_cast<int>(httpResponse->GetStatusCode()));
            return E_FAIL;
        }

        return hr;
    }

    HRESULT Heartbeat(const char* errorCode, const char* status) override
    {
        HRESULT hr = S_OK;

        // Form request
        ComPtr<IHttpRequest> request;
        CHECKHR(CreateHttpRequest(request.AddressOf()));
        request->SetPath("publishHeartbeat");
        request->SetMethod(MethodType::POST);
        request->AddHeader("Content-Type", "application/json");
        request->AddHeader("User-Agent", USER_AGENT);

        nlohmann::json jObj;
        jObj["nodeId"] = _nodeUid;
        jObj["errorCode"] = errorCode;
        jObj["status"] = status;

        ComPtr<IBuffer> body;
        CHECKHR(CreateBufferFromString(body.AddressOf(), jObj.dump().c_str()));
        request->AddBody(body);

        // Make REST call
        ComPtr<IHttpResponse> response;
        CHECKHR(_restApi->MakeRequest(response.AddressOf(), request));

        // Read response
        ComPtr<IBuffer> responseBody;
        CHECKHR(response->GetBody(responseBody.AddressOf()));

        TraceVerbose("Response from Heartbeat: %s", responseBody->AsString());
        if (response->GetStatusCode() != HttpStatusCode::OK)
        {
            TraceError("GetCredentials failed with error code %d", static_cast<int>(response->GetStatusCode()));
            return E_FAIL;
        }

        return hr;
    }

    HRESULT GetCredentials(IBuffer** ppObj) override
    {
        HRESULT hr = S_OK;

         // Form request
        ComPtr<IHttpRequest> request;
        CHECKHR(CreateHttpRequest(request.AddressOf()));
        request->SetPath("getCredentials");
        request->SetMethod(MethodType::GET);
        request->AddHeader("Content-Type", "application/json");
        request->AddHeader("User-Agent", USER_AGENT);

        // Make REST call
        ComPtr<IHttpResponse> response;
        CHECKHR(_restApi->MakeRequest(response.AddressOf(), request));

        // Read response
        response->GetBody(ppObj);
        TraceVerbose("Response from GetCredentials: %s", (*ppObj)->AsString());
        if (response->GetStatusCode() != HttpStatusCode::OK)
        {
            TraceError("GetCredentials failed with error code %d", static_cast<int>(response->GetStatusCode()));
            return E_FAIL;
        }

        return hr;
    }

    HRESULT GetPorts(IBuffer** ppObj) override
    {
        HRESULT hr = S_OK;

        CHECKNULL(ppObj, E_POINTER);
        ComPtr<IHttpRequest> request;
        CHECKHR(CreateHttpRequest(request.AddressOf()));
        request->SetPath("getPorts");
        request->SetMethod(MethodType::POST);
        request->AddHeader("Content-Type", "application/json");
        request->AddHeader("User-Agent", USER_AGENT);

        nlohmann::json json;
        json["nodeId"] = _nodeUid;

        ComPtr<IBuffer> body;
        CHECKHR(Buffer::CreateFromString(body.AddressOf(), json.dump().c_str()));
        request->AddBody(body);

        ComPtr<IHttpResponse> response;
        CHECKHR(_restApi->MakeRequest(response.AddressOf(), request));

        ComPtr<IBuffer> buffer;
        response->GetBody(buffer.AddressOf());
        TraceVerbose("Response from GetPorts: %s", buffer->AsString());
        if (response->GetStatusCode() != HttpStatusCode::OK)
        {
            TraceError("GetPorts failed with error code %d", static_cast<int>(response->GetStatusCode()));
            return E_FAIL;
        }

        *ppObj = buffer.Detach();
        return hr;
    }

private:
    MDSClient() = default;

    HRESULT Initialize(const char* ip, int32_t port, const char* nodeUid)
    {
        HRESULT hr = S_OK;
        CHECKNULL(ip, E_INVALIDARG);
        CHECKIF(port <= 0, E_INVALIDARG);
        CHECKNULL(nodeUid, E_INVALIDARG);

        _ip = ip;
        _port = port;
        _nodeUid = nodeUid;

        CHECKHR(CreateRESTClient(_restApi.AddressOf(), ip, port));
        return hr;
    }

    std::string _ip;
    int32_t _port = 0;
    std::string _nodeUid;
    std::mutex _mtx;

    ComPtr<IRestAPI> _restApi;
};

DLLAPI HRESULT CreateMDSClient(IMDSClient** ppObj, const char* ip, int32_t port, const char* nodeUid)
{
    return MDSClient::Create(ppObj, ip, port, nodeUid);
}