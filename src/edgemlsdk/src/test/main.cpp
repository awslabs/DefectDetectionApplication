#include <gtest/gtest.h>

#include <Panorama/apidefs.h>
#include <Panorama/trace.h>
#include <Panorama/comptr.h>

#define MAIN
#include "TestUtils.h"


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

    ::testing::Environment* test_env = CreateGlobalSetup();
    if(test_env != nullptr)
    {
        // GTest takes ownership of the pointer here, 
        // so need to delete ourselves
        ::testing::AddGlobalTestEnvironment(test_env);
    }
    
    test_argc = argc;
    test_argv = argv;

    Tracer::SetTraceLevel(TraceLevel::Information);
    GetOpts(argc, argv);
    
    ComPtr<ITraceListener> console; 
    CreateConsoleTraceListener(console.AddressOf()); 
    Panorama::AddTraceListener(console);
    
    enable_memcheck(true);
    int ret = RUN_ALL_TESTS();
    if(FAILED(memcheck()))
    {
        return 1;
    }

    Panorama::RemoveTraceListener(console);
    return ret;
}