#include <nlohmann/json.hpp>

#include <Panorama/comptr.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/aws.h>

#include "gst_runner.h"

using namespace Panorama;

DLLAPI HRESULT CreateVariableExpansion(IVariableExpansion** ppObj, IStringProperty* property, ICredentialProvider* cred_provider)
{
    HRESULT hr = S_OK;
    CHECKNULL(ppObj, E_POINTER);
    CHECKNULL(property, E_INVALIDARG);

    // determine what type of variable expansion is needed
    std::string json = property->Get();
    CHECKIF(nlohmann::json::accept(json.c_str()) == false, E_INVALIDARG);
    nlohmann::json obj = nlohmann::json::parse(json.c_str());
    CHECKIF(obj.contains("type") == false, E_INVALIDARG);

    // Return the appropriate expansion implementation
    if(std::string(obj["type"]).compare("string") == 0)
    {
        return CreateStringExpansion(ppObj, property);
    }
    else if(std::string(obj["type"]).compare("secretsmanager") == 0)
    {
        return CreateSecretsManagerExpansion(ppObj, property, cred_provider);
    }
    else if(std::string(obj["type"]).compare("s3") == 0)
    {
        return CreateS3Expansion(ppObj, property, cred_provider);
    }

    return E_NOTIMPL;
}