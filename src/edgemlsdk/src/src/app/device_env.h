#ifndef __DEVICE_ENV_VARS_H__
#define __DEVICE_ENV_VARS_H__

#include <string>
#include <vector>
#include <sstream>

#include <env_vars.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/trace.h>
#include <Panorama/credentials.h>
#include <misc.h>

#define NODE_UID_ENV "Node_Uid"
#define REMOTE_DEVICE_ENV "grpcDevice"

#define MDS_IP_ADDR "http://169.254.170.2"
#define MDS_IP_ADDR_ENV "MDS_IP_OVERRIDE"

#define METATADATA_RELATIVE_URI_ENV "AWS_CONTAINER_METADATA_RELATIVE_URI"
#define USER_AGENT "Node Runtime"
#define NODE_UID_ENV "Node_Uid"
#define LOG_FILE_OVERRIDE_ENV "LOG_FILE_OVERRIDE"
#define DEFAULT_LOG_FILE "/opt/aws/panorama/logs/app.log"

inline std::string GetNodeId()
{
    return GetEnvVar(NODE_UID_ENV);
}

inline std::string GetGrpcDevice()
{
    return GetEnvVar(REMOTE_DEVICE_ENV);
}

inline bool OnPanoAppliance()
{
    // As seen in AwsOmniBundle\AwsOmniContainerPlugin\scripts\omni_podman.sh
    // the following environment variable is set when launching the container, so we'll
    // use this as a signal indicating we are on the panorama appliance.
    std::string appGraphUid = GetEnvVar("AppGraph_Uid");
    return !appGraphUid.empty();
}

namespace Panorama
{
    enum class ClientType
    {
        // Not connected to any known host environment
        StandAlone = 0,

        // Try to connect to the local client
        LocalClient,

        // Try to connect to the appliance
        PanoramaClient,

        // Running as a Greengrass component
        GreenGrassClient
    };

    inline HRESULT ParseIpPort(std::string& ip, int32_t& port, std::string ip_port)
    {
        HRESULT hr = S_OK;

        std::vector<std::string> split = SplitString(ip_port, ':');
        CHECKIF_MSG(split.size() != 2, E_INVALIDARG, "Ip-Port string is not in the correct format <ip>:<port>");
        ip = split[0];

        // validate port is a number before trying to convert to int
        std::string::const_iterator it = split[1].begin();
        while (it != split[1].end() && std::isdigit(*it)) ++it;
        CHECKIF_MSG(it != split[1].end(), E_INVALIDARG, "Port does not appear to be an integer");

        port = std::stoi(split[1]);
        CHECKIF_MSG(port <= 0, E_INVALIDARG, "Port should be positive");

        return hr;
    }

    inline HRESULT GetMDSIp(std::string& ip, int32_t& port, bool mock=false)
    {
        HRESULT hr = S_OK;
        
        if(mock == false)
        {
            // Get the metadata port
            std::string metadataRelativeUri = GetEnvVar(METATADATA_RELATIVE_URI_ENV);
            CHECKIF_MSG(metadataRelativeUri.empty(), E_INVALID_STATE, METATADATA_RELATIVE_URI_ENV" environment variable not found");
            TraceVerbose(METATADATA_RELATIVE_URI_ENV" = %s", metadataRelativeUri.c_str());
            port = atoi(std::regex_replace(metadataRelativeUri.c_str(), std::regex(R"([^\d])"), "").c_str());
            CHECKIF_MSG(port == 0, E_INVALID_STATE, "Port number could not be parsed");

            // Hardcoded to 169.254.170.2 on Panorama Device
            ip = MDS_IP_ADDR;
        }
        else
        {
            // Get the IP and Port if connecting to Mock MDS Service
            // Which is specified by setting "MDS_IP_OVERRIDE" environment variable to "<ip>:<port>"
            std::stringstream ss;
            std::vector<std::string> mds;

            ss << GetEnvVar(MDS_IP_ADDR_ENV);
            CHECKIF_MSG(ss.str().empty(), E_INVALID_STATE, MDS_IP_ADDR_ENV" not set");
            CHECKHR(ParseIpPort(ip, port, ss.str()));
        }

        return S_OK;
    }

    inline ClientType DetermineClientType()
    {
        // Determine if we need to attempt to connect to the device or not.
        // If we are running on the appliance OR the user has set the MDS_IP_OVERRIDE
        // environment variable then we will attempt to connect to the device
        if(OnPanoAppliance())
        {
            return ClientType::PanoramaClient;
        }
        else if(GetEnvVar(MDS_IP_ADDR_ENV).empty() == false)
        {
            return ClientType::LocalClient;
        }
        // else if(SUCCEEDED(this->GetBooleanProperty(ggv2Property.AddressOf(), "ggv2")))
        // {
        //     return ClientType::GreenGrassClient;
        // }
        else
        {
            return ClientType::StandAlone;
        }
    }

    DLLAPI HRESULT CreatePlatformCredentialProvider(ICredentialProvider** ppObj);
}

#endif