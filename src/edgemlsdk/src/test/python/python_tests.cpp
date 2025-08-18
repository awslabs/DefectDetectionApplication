#include <thread>
#include <fstream>
#include <sstream>

#include <gtest/gtest.h>
#include <Panorama/comobj.h>
#include <Panorama/comptr.h>
#include <Panorama/python.h>
#include <Panorama/flowcontrol.h>

#define GTEST_SETUP
#include <TestUtils.h>

#ifdef _DEBUG
#undef _DEBUG
#include <Python.h>
#define _DEBUG
#else
#include <Python.h>
#endif

using namespace std;
using namespace Panorama;

class GlobalSetup : public ::testing::Environment 
{
public:
    void SetUp() override 
    {
        Py_Initialize();
    }

    void TearDown() override 
    {
        Py_Finalize();
    }
};

::testing::Environment* CreateGlobalSetup()
{
    return new GlobalSetup();
}

PyObject* StartCoverage()
{
    // Open and execute the setup coverage Python file
    std::string coverageFilePath = PythonTestScript("setup_coverage.py");
    FILE* coverageFile = fopen(coverageFilePath.c_str(), "r");
    PyRun_SimpleFile(coverageFile, coverageFilePath.c_str());

    // get main module
    PyObject* mainModule = PyImport_ImportModule("__main__");

    // create instance of coverage class
    PyObject* coverageClass = PyObject_GetAttrString(mainModule, "PythonCoverage");
    PyObject* coverageClassInstance = PyObject_CallFunction(coverageClass, "s", BuildDirectory().c_str());
    if(PyErr_Occurred())
    {
        PyErr_Print();
    }
    return coverageClassInstance;
}

HRESULT RunScriptWithDebugger(std::string script, std::stringstream* configurationVars = nullptr)
{
    HRESULT hr = S_OK;

    SetEnvVar("BUILD_DIRECTORY", BuildDirectory().c_str());
    AppendPythonPath(PythonLibraryDirectory().c_str());
    AppendPythonPath( (BuildDirectory()+"/lib").c_str());

    
    std::ifstream fileIn(script, ios_base::in);
    if (!fileIn.is_open()) {
        TraceError("cannot open file %s", script.c_str());
        return E_FAIL;
    }

    CHECKHR(fileIn.is_open());
    std::stringstream ss;
    if (configurationVars)
    {
        ss << configurationVars->str();
    }

    PyObject* coverageClassInstance = StartCoverage();

    // run test
    ss << fileIn.rdbuf();
    hr = PyRun_SimpleString(ss.str().c_str()) == 0 ? S_OK : E_FAIL;

    // stop coverage
    PyObject* return_val = PyObject_CallMethod(coverageClassInstance, "stop_coverage", NULL);

    return hr;
}

TEST(Python, TraceTests)
{
    HRESULT hr = S_OK;
    ASSERT_S(RunScriptWithDebugger(PythonTestScript("trace_tests.py")));
}

TEST(Python, PropertyTests)
{
    HRESULT hr = S_OK;

    std::string env_variables = 
        "import os\n"
        "os.environ['BUILD_DIRECTORY'] = '" + BuildDirectory() + "'\n";

    std::stringstream ss;
    ss << env_variables;

    ASSERT_S(RunScriptWithDebugger(PythonTestScript("property_tests.py"), &ss));
}

TEST(Python, DeviceApplicationTests)
{
    HRESULT hr = S_OK;
    ASSERT_S(RunScriptWithDebugger(PythonTestScript("device_application_tests.py")));
}

TEST(Python, MqttProtocolClientTests)
{
    HRESULT hr = S_OK;
    ASSERT_S(RunScriptWithDebugger(PythonTestScript("mqtt_protocol_client_tests.py")));
}

TEST(Python, S3ProtocolClientTests)
{
    HRESULT hr = S_OK;
    ASSERT_S(RunScriptWithDebugger(PythonTestScript("s3_protocol_client_tests.py")));
}

TEST(Python, FileProtocolClientTests)
{
    HRESULT hr = S_OK;
    ASSERT_S(RunScriptWithDebugger(PythonTestScript("file_protocol_client_tests.py")));
}

TEST(Python, MessageBrokerTests)
{
    HRESULT hr = S_OK;
    ASSERT_S(RunScriptWithDebugger(PythonTestScript("message_broker_tests.py")));
}

TEST(Python, GstTests)
{
    HRESULT hr = S_OK;
    std::string env_variables =
        "import os\n"
        "os.environ['GST_PLUGIN_PATH'] = '" + BuildDirectory() + "/lib/" +"'\n"
        "os.environ['MODEL_REPO_DIR'] = '" + BuildDirectory() + "/bin/model_repo/" +"'\n"
        "os.environ['TRITON_INSTALL_DIR'] = '/dependencies/server/build/opt/tritonserver/'\n";

    std::stringstream ss;
    ss << env_variables;
    ASSERT_S(RunScriptWithDebugger(PythonTestScript("gst_tests.py"),&ss));
}

TEST(Python, PythonProtocolClient)
{
    HRESULT hr = S_OK;
    ASSERT_S(RunScriptWithDebugger(PythonTestScript("python_protocol_client_tests.py")));
}

TEST(Python, QueryInterfaceTests)
{
    HRESULT hr = S_OK;
    ASSERT_S(RunScriptWithDebugger(PythonTestScript("query_interface_tests.py")));
}

TEST(Python, TritonTests)
{
    HRESULT hr = S_OK;

    std::stringstream ss;

    std::string env_variables = 
        "import os\n"
        "os.environ['BUILD_DIRECTORY'] = '" + BuildDirectory() + "'\n"
        "os.environ['TRITON_INSTALL_DIRECTORY'] = '" + TRITON_INSTALL_DIR + "'\n";
    ss << env_variables;

    ASSERT_S(RunScriptWithDebugger(PythonTestScript("triton_tests.py"), &ss));
}