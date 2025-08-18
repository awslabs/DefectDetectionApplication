#ifndef __TEST_APP_REQUEST_HANDLER_H__
#define __TEST_APP_REQUEST_HANDLER_H__

#include <Panorama/eventing.h>
#include <tools/mockdevice/mock_panorama_device.h>

namespace Panorama
{
    class AppRequestHandler : public UnknownImpl<IAppRequestHandler>
    {
    public:
        AppRequestHandler();
        const char* GetCredentials() override;
        void SetCredentials(const std::string& creds);
        void SetPorts(const std::string& ports);
        const char* GetPorts(const char* nodeId) override;
        void OnAnnounceSelf(const char* nodeId, const char *version) override;
        void OnHeartbeat(const char* nodeId, const char* errorCode, const char* status) override;
        void OnTraceMessage(TraceLevel level, Timestamp timestamp, int32_t line, const char* file, const char* message) override;

        AutoResetEvent GetCredentialsCalled;
        
        AutoResetEvent AnnounceSelfCalled;
        std::string AnnounceSelfNodeId;
        std::string AnnounceSelfVersion;

        AutoResetEvent HeartbeatCalled;
        std::string HeartbeatNodeId;
        std::string HeartbeatErrorCode;
        std::string HeartbeatStatus;

        AutoResetEvent TraceCalled;
        TraceLevel Received_Level;
        Timestamp Received_Timestamp;
        int32_t Received_Line;
        std::string Received_File;
        std::string Received_Message;

    private:
        std::string _creds;
        std::string _ports;
    };

    DLLAPI void GetAppRequestHandler(AppRequestHandler** ppObj);
}

#endif