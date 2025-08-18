#ifndef __TEST_UTILS_H__
#define __TEST_UTILS_H__

#include <gtest/gtest.h>
#include <sstream>

#include <Panorama/apidefs.h>
#include <Panorama/trace.h>
#include <Panorama/eventing.h>
#include <Panorama/comptr.h>
#include <Panorama/properties.h>
#include <env_vars.h>

#define ASSERT_S(X) hr = X; ASSERT_TRUE(SUCCEEDED(hr))
#define ASSERT_F(X) hr = X; ASSERT_TRUE(FAILED(hr))

#define EXPECT_S(X) hr = X; EXPECT_TRUE(SUCCEEDED(hr))
#define EXPECT_F(X) hr = X; EXPECT_TRUE(FAILED(hr))

extern int test_argc;
extern char **test_argv;

using namespace std;

class CommandLineArgs
{
public:
    ~CommandLineArgs()
    {
        if(argv != nullptr)
        {
            for(int i = 0; i < argc; i++)
            {
                delete[] argv[i];
            }

            delete[] argv;
        }
    }

    int Count()
    {
        return argc;
    }

    char** Values()
    {
        return argv;
    }

    int argc;
    char** argv;
};

inline bool OnBuildServer()
{
    return !(GetEnvVar("BUILD_SERVER").empty());
}

inline std::string BuildDirectory()
{
    if(OnBuildServer() == false)
    {
        return BUILD_DIR;
    }
    
    std::string arch = GetEnvVar("ARCH");
    std::stringstream ss;
    ss << GetEnvVar("BUILD_DIR") << "/" << GetMajorMinorVersionString() << "/Release/" << arch;
    return ss.str();
}

inline std::string PythonLibraryDirectory()
{
    return BuildDirectory() + "/lib/python_package/src";
}

inline std::string TestDataDirectory()
{
    //return TEST_DATA_DIR;
    return "";
}

inline std::string PythonTestDirectory()
{
    std::stringstream ss;
    if(OnBuildServer() == false)
    {
        ss << BuildDirectory() << "/bin/";
    }
    else
    {
        ss << BuildDirectory() << "/tests/";
    }

    return ss.str();
}

inline std::string PythonTestScript(const char* filename)
{
    std::stringstream ss;
    ss << PythonTestDirectory() << filename;
    return ss.str();
}

inline std::string DependencyDirectory()
{
    //return DEPENDENCIES_DIR;
    return "";
}

inline CommandLineArgs CreateCommandLineArgs(const std::string& inputString)
{
    CommandLineArgs cl_args;

    std::vector<std::string> args;
    std::string s;

    std::stringstream ss;
    ss << inputString;

    while (getline(ss, s, ' ')) 
    {
        args.push_back(s);
    }

    cl_args.argc = args.size();
    cl_args.argv = new char*[args.size()];

    char** ptr = cl_args.argv;
    
    for (int i = 0; i < args.size(); i++) 
    {
        ptr[i] = new char[args[i].length() + 1];
        strcpy(ptr[i], args[i].c_str());
    }

    return cl_args;
}

#ifdef MAIN
::testing::Environment* CreateGlobalSetup();
#elif defined(GTEST_SETUP)
// to be defined in <test>.cpp file
#else
::testing::Environment* CreateGlobalSetup()
{
    return nullptr;
}
#endif

#endif
