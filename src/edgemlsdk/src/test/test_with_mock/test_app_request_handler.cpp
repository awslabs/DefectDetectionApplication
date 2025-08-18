#include <mutex>
#include "test_app_request_handler.h"

using namespace Panorama;

AppRequestHandler::AppRequestHandler()
{
    _creds = "{}";
    _ports = "{\"inputPortList\":[]}";
    Received_Level=TraceLevel::Error;
    Received_Timestamp = 0;
    Received_Line = 0;
}

const char* AppRequestHandler::GetCredentials()
{
    GetCredentialsCalled.Set();
    return _creds.c_str();
}

void AppRequestHandler::SetCredentials(const std::string& creds)
{
    _creds = creds;
}

void AppRequestHandler::SetPorts(const std::string& ports)
{
    _ports = ports;
}

const char* AppRequestHandler::GetPorts(const char* nodeId)
{
    return _ports.c_str();
}

void AppRequestHandler::OnAnnounceSelf(const char* nodeId, const char *version) 
{
    AnnounceSelfNodeId = nodeId;
    AnnounceSelfVersion = version;
    AnnounceSelfCalled.Set();
}

void AppRequestHandler::OnHeartbeat(const char* nodeId, const char* errorCode, const char* status)
{
    HeartbeatNodeId = nodeId;
    HeartbeatErrorCode = errorCode;
    HeartbeatStatus = status;
    HeartbeatCalled.Set();
}

void AppRequestHandler::OnTraceMessage(TraceLevel level, Timestamp timestamp, int32_t line, const char* file, const char* message)
{
    Received_Level = level;
    Received_Line = line;
    Received_Timestamp = timestamp;
    Received_File = file;
    Received_Message = message;
    TraceCalled.Set();
}

static AppRequestHandler* instance;
static std::mutex mtx;
DLLAPI void GetAppRequestHandler(AppRequestHandler** ppObj)
{
    {
        std::lock_guard<std::mutex> lk(mtx);
        if(instance == nullptr)
        {
            instance = new AppRequestHandler();
        }
    }

    instance->AddRef();
    *ppObj = instance;
}