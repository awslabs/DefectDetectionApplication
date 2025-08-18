#include <unistd.h>
#include <fstream>
#include <nlohmann/json.hpp>

#include <aws/core/Aws.h>

#include <Panorama/app.h>
#include <Panorama/comptr.h>
#include <Panorama/trace.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/aws.h>

#include "../mock_panorama_device.h"
#include "options.h"

using namespace Panorama;
HRESULT CreateAppRequestHandler(IAppRequestHandler** ppObj, const std::string& appGraphFile);

int main(int argc, char* argv[])
{
    ADD_CONSOLE_TRACE;
    ComPtr<IAwsContext> aws_context = Panorama_Aws::AwsContext();

    Options options = GetOpts(argc, argv);
    TraceInfo("%s", options.ToString().c_str());
    
    HRESULT hr = S_OK;
    ComPtr<IPanoramaDevice> device;
    CHECKHR(CreatePanoramaDevice(device.AddressOf()));

    ComPtr<IAppRequestHandler> handler;
    CHECKHR(CreateAppRequestHandler(handler.AddressOf(), options.ConfigFile));

    CHECKHR(device->SetRequestHandler(handler));
    CHECKHR(device->Start(options.Port));

    printf("Hit any key to exit\n");
    int32_t key = getchar();
    device->Stop();
}