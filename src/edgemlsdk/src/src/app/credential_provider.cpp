
#include <Panorama/app.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/aws.h>
#include "device_env.h"

using namespace Panorama;

HRESULT CreateDefaultCredentialProvider(ICredentialProvider** ppObj)
{
    return CreateDefaultAwsCredentialProvider(ppObj);
}

HRESULT CreateMDSCredentialProvider(ICredentialProvider** ppObj, bool mock)
{
    HRESULT hr = S_OK;
    std::string ip;
    int port;

    ComPtr<IMDSClient> client;

    std::string nodeUid = GetEnvVar(NODE_UID_ENV, "");
    CHECKHR(GetMDSIp(ip, port, mock));
    CHECKHR(CreateMDSClient(client.AddressOf(), ip.c_str(), port, nodeUid.c_str()));
    CHECKHR(CreateMDSCredentialProvider(ppObj, client));

    return hr;
}

DLLAPI HRESULT CreatePlatformCredentialProvider(ICredentialProvider** ppObj)
{
    switch(DetermineClientType())
    {
        case ClientType::StandAlone:
            return CreateDefaultCredentialProvider(ppObj);
            break;
        case ClientType::LocalClient:
            return CreateMDSCredentialProvider(ppObj, true);
        case ClientType::PanoramaClient:
            return CreateMDSCredentialProvider(ppObj, false);
        default:
            return E_NOTIMPL;
    }
}