#include <gtest/gtest.h>

#include <Panorama/apidefs.h>
#include <Panorama/trace.h>
#include <Panorama/comptr.h>
#include <tools/mockdevice/mock_panorama_device.h>

#include "test_app_request_handler.h"

using namespace Panorama;

// pass arguments to individual tests
int test_argc;
char **test_argv;

void GetOpts(int argc, char* argv[])
{
    int opt;
    while ((opt = getopt(argc, argv, "l:")) != -1) 
    {
        switch (opt) 
        {
        case 'l':
            Tracer::SetTraceLevel(static_cast<TraceLevel>(atoi(optarg)));
            break;
        default:
            exit(1);
        }
    }
}

DLLAPI int main(int argc, char** argv) {
    ::testing::InitGoogleTest(&argc, argv);
    test_argc = argc;
    test_argv = argv;

    Tracer::SetTraceLevel(TraceLevel::Information);
    GetOpts(argc, argv);
    ADD_CONSOLE_TRACE;

    ComPtr<AppRequestHandler> handler;
    GetAppRequestHandler(handler.AddressOf());

    ComPtr<IPanoramaDevice> mock;
    CreatePanoramaDevice(mock.AddressOf());
    mock->SetRequestHandler(handler);
    mock->Start(8089);

    int results = RUN_ALL_TESTS();

    mock->Stop();
    return results;
}