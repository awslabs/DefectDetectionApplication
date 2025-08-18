#include <string>
#include <sstream>
#include <thread>

#include <civetweb.h>
#include <nlohmann/json.hpp>

#include <Panorama/comptr.h>
#include <Panorama/trace.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/eventing.h>
#include <Panorama/buffer.h>
#include <misc.h>

#include "mock_panorama_device.h"

// #include <serialize.h>
//#include <subscriber.h>


using namespace Panorama;

#define HTTP_OK int32_t(200)
#define BAD_REQUEST int32_t(400)
#define INTERNAL_SERVER_ERROR int32_t(500)
#define GET_JSON(X) if(GetJson(&X, conn, requestBody) == false) { return mg_send_http_error(conn, BAD_REQUEST, "Invalid JSON"); }
#define CHECK_JSON_KEY(JSON, KEY, RESULT)   \
                                    std::string RESULT;                                                             \
                                    if(JSON.contains(KEY) == false)                                                 \
                                    {                                                                               \
                                        TraceError(KEY" not specified in body");                                    \
                                        return mg_send_http_error(conn, BAD_REQUEST, KEY" not specified in body");  \
                                    }                                                                               \
                                    else                                                                            \
                                    {                                                                               \
                                        RESULT = JSON[KEY];                                                         \
                                    }

class PanoramaDevice : public UnknownImpl<IPanoramaDevice>
{
public:
    static HRESULT Create(IPanoramaDevice** ppObj)
    {
        HRESULT hr = S_OK;

        ComPtr<PanoramaDevice> ptr;
        ptr.Attach(new (std::nothrow) PanoramaDevice());
        CHECKNULL_MSG(ptr, E_OUTOFMEMORY, "Could not allocate new AppControlPlaneServer");
        CHECKHR(ptr->InitializeRESTServer());

        *ppObj = ptr.Detach();
        return hr;
    }

    static bool GetJson(nlohmann::json* pObj, mg_connection* conn, IBuffer* requestBody)
    {
        if(nlohmann::json::accept(requestBody->AsString()))
        {
            *pObj = nlohmann::json::parse(requestBody->AsString());
        }
        else
        {
            TraceError("Invalid JSON");
            return false;
        }

        return true;
    }

    static int32_t AnnounceSelfHandler(PanoramaDevice* device, mg_connection* conn, IBuffer* requestBody)
    {
        CHECKNULL(device, INTERNAL_SERVER_ERROR);
        CHECKNULL(conn, INTERNAL_SERVER_ERROR);
        CHECKNULL(requestBody, INTERNAL_SERVER_ERROR);

        nlohmann::json parse;
        TraceVerbose("Received Announce Self Request: %s", requestBody->AsString());
        GET_JSON(parse);

        CHECK_JSON_KEY(parse, "nodeId", nodeId);
        CHECK_JSON_KEY(parse, "version", version);

        nlohmann::json json;
        json["nodeId"] = nodeId;

        if (device->_handler != nullptr)
        {
            device->_handler->OnAnnounceSelf(nodeId.c_str(), version.c_str());
        }

        std::string body = json.dump();
        mg_send_http_ok(conn, "application/json", body.length());
        mg_write(conn, body.c_str(), body.length());
        return HTTP_OK;
    }

    static int32_t GetPortsHandler(PanoramaDevice* device, mg_connection* conn, IBuffer* requestBody)
    {
        CHECKNULL(device, INTERNAL_SERVER_ERROR);
        CHECKNULL(conn, INTERNAL_SERVER_ERROR);
        CHECKNULL(requestBody, INTERNAL_SERVER_ERROR);

        nlohmann::json parse;
        TraceVerbose("Received GetPorts Request: %s", requestBody->AsString());
        GET_JSON(parse);
        CHECK_JSON_KEY(parse, "nodeId", nodeId);

        std::string inputPortsList;
        if (device->_handler != nullptr)
        {
            inputPortsList = device->_handler->GetPorts(nodeId.c_str());
        }
        else
        {
            inputPortsList = "{}";
        }

        mg_send_http_ok(conn, "application/json", inputPortsList.length());
        mg_write(conn, inputPortsList.c_str(), inputPortsList.length());
        return HTTP_OK;
    }

    static int32_t PublishHeartbeatHandler(PanoramaDevice* device, mg_connection* conn, IBuffer* requestBody)
    {
        CHECKNULL(device, INTERNAL_SERVER_ERROR);
        CHECKNULL(conn, INTERNAL_SERVER_ERROR);
        CHECKNULL(requestBody, INTERNAL_SERVER_ERROR);

        nlohmann::json parse;
        TraceVerbose("Received Publish Heartbeats Request: %s", requestBody->AsString());
        GET_JSON(parse);

        CHECK_JSON_KEY(parse, "nodeId", nodeId);
        CHECK_JSON_KEY(parse, "errorCode", errorCode);

        std::string status = "";
        if(parse.contains("status"))
        {
            status = parse["status"];
        }

        if (device->_handler != nullptr)
        {
            device->_handler->OnHeartbeat(nodeId.c_str(), errorCode.c_str(), status.c_str());
        }

        nlohmann::json json;
        json["nodeId"] = nodeId;
        json["errorCode"] = errorCode;
        json["status"] = status;

        std::string body = json.dump();
        mg_send_http_ok(conn, "application/json", body.length());
        mg_write(conn, body.c_str(), body.length());
        return HTTP_OK;
    }

    static int32_t GetCredentialsHandler(PanoramaDevice* device, mg_connection* conn, IBuffer* requestBody)
    {
        CHECKNULL(device, INTERNAL_SERVER_ERROR);
        CHECKNULL(conn, INTERNAL_SERVER_ERROR);

        TraceVerbose("Received Credentials Request");
        std::string credentials = device->_handler->GetCredentials();
        mg_send_http_ok(conn, "application/json", credentials.length());
        mg_write(conn, credentials.c_str(), credentials.length());
        return HTTP_OK;
    }

    static int32_t GetTraceHandler(PanoramaDevice* device, mg_connection* conn, IBuffer* requestBody)
    {
        CHECKNULL(device, INTERNAL_SERVER_ERROR);
        CHECKNULL(conn, INTERNAL_SERVER_ERROR);

        if(requestBody != nullptr)
        {
            if(nlohmann::json::accept(requestBody->AsString()))
            {
                nlohmann::json msg = nlohmann::json::parse(requestBody->AsString());
                int64_t timestamp = ValidateJsonProperty<int64_t>(msg, "Timestamp");
                if(
                    ValidateJsonProperty<int32_t>(msg, "Level") &&
                    ValidateJsonProperty<int64_t>(msg, "Timestamp") &&
                    ValidateJsonProperty<int32_t>(msg, "Line") &&
                    ValidateJsonProperty<const char*>(msg, "File") &&
                    ValidateJsonProperty<const char*>(msg, "Message")
                )
                {
                    TraceLevel level = static_cast<TraceLevel>(msg["Level"]);
                    Timestamp ts = msg["Timestamp"];
                    int32_t line = msg["Line"];
                    std::string file = msg["File"];
                    std::string message = msg["Message"];
                    device->_handler->OnTraceMessage(level, ts, line, file.c_str(), message.c_str());
                }
            }
        }

        std::string response = "{}";
        mg_send_http_ok(conn, "application/json", response.length());
        mg_write(conn, response.c_str(), response.length());
        return HTTP_OK;
    }

    static int begin_request_handler(mg_connection *conn)
    {
        const struct mg_request_info *request_info = mg_get_request_info(conn);
        PanoramaDevice* device = reinterpret_cast<PanoramaDevice*>(request_info->user_data);
        CHECKNULL(device, INTERNAL_SERVER_ERROR);

        ComPtr<IBuffer> requestBody;
        if(strcmp(request_info->request_method, "POST") == 0)
        {
            // Read the body of the content
            int32_t contentLength = request_info->content_length;
            if(FAILED(CreateBuffer(requestBody.AddressOf(), contentLength)))
            {
                return 0;
            }

            uint8_t* ptr = requestBody->Data();
            while(contentLength > 0)
            {
                int32_t bytesRead = std::max(mg_read(conn, ptr, contentLength), 0);
                ptr += bytesRead;
                contentLength -= bytesRead;
            }
        }
        
        if(strcmp(request_info->local_uri, "/announceSelf") == 0)
        {
            return AnnounceSelfHandler(device, conn, requestBody);
        }
        else if(strcmp(request_info->local_uri, "/getPorts") == 0)
        {
            return GetPortsHandler(device, conn, requestBody);
        }
        else if(strcmp(request_info->local_uri, "/publishHeartbeat") == 0)
        {
            return PublishHeartbeatHandler(device, conn, requestBody);
        }
        else if(strcmp(request_info->local_uri, "/getCredentials") == 0)
        {
            return GetCredentialsHandler(device, conn, requestBody);
        }
        else if(strcmp(request_info->local_uri, "/trace") == 0)
        {
            return GetTraceHandler(device, conn, requestBody);
        }
        
        return mg_send_http_error(conn, BAD_REQUEST, "Unknown method");
    }

    ~PanoramaDevice()
    {
        if (_restThread.joinable())
        {
            _restThread.join();
        }
    }

    HRESULT SetRequestHandler(IAppRequestHandler* handler) override
    {
        HRESULT hr = S_OK;
        CHECKNULL(handler, E_INVALIDARG);
        _handler = handler;
        return hr;
    }

    std::thread t;

    HRESULT Start(int portListen) override
    {
        _ctx = nullptr;
        _port = portListen;

        _restThread = std::thread([this]()
        {
            std::string portStr = std::to_string(_port);

            const char *options[] = {"listening_ports", portStr.c_str(), nullptr};
            _ctx = mg_start(&_callbacks, this, options);
            if(_ctx == nullptr)
            {
                TraceError("Cannot start CivetWeb - mg_start failed.");
                return;
            }

            _stopRestServer.Wait();
            TraceInfo("Stopping RESTful server");
            mg_stop(_ctx);
        });

        // Wait for rest server to be stood up (up to 3 seconds)
        int attempts = 0;
        do
        {
            ThreadSleep(100);
        } while (_ctx == nullptr && attempts < 30);

        CHECKNULL_MSG(_ctx, E_FAIL, "Timed out creating rest server");
        return S_OK;
    }

    void Stop() override
    {
        _stopRestServer.Set();
    }

    HRESULT InitializeRESTServer()
    {
        // Init libcivetweb.
        mg_init_library(0);

        // Callback will print error messages to console
        memset(&_callbacks, 0, sizeof(_callbacks));
        _callbacks.begin_request = begin_request_handler;

        return S_OK;
    }

private:
    PanoramaDevice() = default;

    ComPtr<IAppRequestHandler> _handler;
    std::map<std::string, std::map<std::string, ComPtr<IBuffer>>> _properties;

    // REST Server
    mg_callbacks _callbacks;
    mg_context *_ctx;
    ManualResetEvent _stopRestServer;
    std::thread _restThread;
    int32_t _port;
};

DLLAPI HRESULT CreatePanoramaDevice(IPanoramaDevice** ppObj)
{
    return PanoramaDevice::Create(ppObj);
}