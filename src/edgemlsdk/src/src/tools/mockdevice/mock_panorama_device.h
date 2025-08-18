#ifndef __MOCK_PANORAMA_DEVICE_H__
#define __MOCK_PANORAMA_DEVICE_H__

#include <Panorama/comobj.h>
#include <Panorama/credentials.h>

namespace Panorama
{
    DEF_INTERFACE(IAppRequestHandler, "{d8346d53-79c4-4c7b-8e52-a50f06d66a77}", IUnknownAlias)
    {
        virtual const char* GetCredentials() = 0;
        virtual const char* GetPorts(const char* nodeId) = 0;
        virtual void OnAnnounceSelf(const char* nodeId, const char *version) = 0;
        virtual void OnHeartbeat(const char* nodeId, const char* errorCode, const char* status) = 0;
        virtual void OnTraceMessage(TraceLevel level, Timestamp timestamp, int32_t line, const char* file, const char* message) = 0;
    };

    DEF_INTERFACE(IPanoramaDevice, "{27cc5bd0-1b01-4e82-8cff-29942de4df00}", IUnknownAlias)
    {
        virtual HRESULT Start(int listenPort = 0) = 0;
        virtual void Stop() = 0;
        virtual HRESULT SetRequestHandler(IAppRequestHandler* handler) = 0;
    };

    DLLAPI HRESULT CreatePanoramaDevice(IPanoramaDevice** ppObj);
    DLLAPI HRESULT CreateMockPanoramaDeviceCredentialProvider(ICredentialProvider** ppObj);
}

#endif