// standard headers
#include <thread>

// depednencies headers
#include <gtest/gtest.h>

// Panorama public headers
#include <Panorama/comobj.h>
#include <Panorama/comptr.h>
#include <Panorama/eventing.h>
#include <Panorama/app.h>
#include <Panorama/trace.h>

// Panorama private headers
//#include <tools/mockdevice/mock_panorama_device.h>
#include <TestUtils.h>
#include "../test_with_mock/test_app_request_handler.h"

using namespace std;
using namespace Panorama;


TEST(PanoramAppTests, MDS_GetCredentials)
{
    HRESULT hr = S_OK;

    ComPtr<AppRequestHandler> handler;
    GetAppRequestHandler(handler.AddressOf());

    ComPtr<ITraceListener> listener;
    ASSERT_S(Tracer::CreateHttpTraceListener(listener.AddressOf(), "127.0.0.1", 8089));
    listener->WriteMessage(TraceLevel::Verbose, NowAsTimestamp(), 1, "foo", "hello world");

    ASSERT_TRUE(handler->TraceCalled.WaitFor(3000));
}