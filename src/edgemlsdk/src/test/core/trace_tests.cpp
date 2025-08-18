
#include <thread>
#include <fstream>

#include <gtest/gtest.h>

#include <Panorama/trace.h>
#include <Panorama/comptr.h>
#include <Panorama/eventing.h>

#include <TestUtils.h>
#include <filesystem_safe.h>

using namespace std;
using namespace Panorama;

TEST(TraceListenerTests, FileTraceListener)
{
    std::string filename = BuildDirectory() + "/fileTraceListener.txt";

    // delete contents of the file
    std::ofstream fstream = std::ofstream(filename, std::ofstream::out);
    fstream.close();

    ComPtr<ITraceListener> file;
    CreateFileTraceListener(file.AddressOf(), filename.c_str(), 10000, 1);
    Tracer::AddTraceListener(file);
    
    ManualResetEvent endTest;
    std::thread t = std::thread([&]
        {
            while (endTest.WaitFor(0) == false)
            {
                ifstream file(filename, ios::binary | ios::ate);
                if (file.tellg() > 10)
                {
                    endTest.Set();
                }

                ThreadSleep(100);
            }
        });

    int x = 0;
    while (endTest.WaitFor(0) == false && x < 5)
    {
        TraceInfo("Hello World: %d", x);
        x++;
        ThreadSleep(1000);
    }

    endTest.Set();
    t.join();
    ASSERT_TRUE(x < 5);
}

bool ValidateContents(std::string file, std::string contents)
{
    std::ifstream fstream(file);
    std::stringstream ss;
    ss << fstream.rdbuf();

    return ss.str().find(contents) != ss.str().npos;
}

TEST(TraceListenerTests, FileTraceListenerRollover)
{
    std::string filename = BuildDirectory() + "/fileTraceListenerRollover.txt";

    // Delete files for clean test
    fs::remove(BuildDirectory() + "/fileTraceListenerRollover.txt");
    fs::remove(BuildDirectory() + "/fileTraceListenerRollover.txt1");
    fs::remove(BuildDirectory() + "/fileTraceListenerRollover.txt2");
    fs::remove(BuildDirectory() + "/fileTraceListenerRollover.txt3");

    ComPtr<ITraceListener> file;
    CreateFileTraceListener(file.AddressOf(), filename.c_str(), 75, 3);
    Tracer::AddTraceListener(file);
    
    TraceInfo("hello");
    ThreadSleep(250); // should be plenty of time for the file to have been written
    
    ASSERT_TRUE(fs::exists(BuildDirectory() + "/fileTraceListenerRollover.txt"));
    ASSERT_FALSE(fs::exists(BuildDirectory() + "/fileTraceListenerRollover.txt1"));
    ASSERT_FALSE(fs::exists(BuildDirectory() + "/fileTraceListenerRollover.txt2"));
    ASSERT_FALSE(fs::exists(BuildDirectory() + "/fileTraceListenerRollover.txt3"));
    ASSERT_TRUE(ValidateContents(BuildDirectory() + "/fileTraceListenerRollover.txt", "hello"));

    TraceInfo("world");
    ThreadSleep(250); // should be plenty of time for the file to have been written

    ASSERT_TRUE(fs::exists(BuildDirectory() + "/fileTraceListenerRollover.txt"));
    ASSERT_TRUE(fs::exists(BuildDirectory() + "/fileTraceListenerRollover.txt1"));
    ASSERT_FALSE(fs::exists(BuildDirectory() + "/fileTraceListenerRollover.txt2"));
    ASSERT_FALSE(fs::exists(BuildDirectory() + "/fileTraceListenerRollover.txt3"));
    ASSERT_TRUE(ValidateContents(BuildDirectory() + "/fileTraceListenerRollover.txt", "world"));
    ASSERT_TRUE(ValidateContents(BuildDirectory() + "/fileTraceListenerRollover.txt1", "hello"));

    TraceInfo("test");
    ThreadSleep(250); // should be plenty of time for the file to have been written

    ASSERT_TRUE(fs::exists(BuildDirectory() + "/fileTraceListenerRollover.txt"));
    ASSERT_TRUE(fs::exists(BuildDirectory() + "/fileTraceListenerRollover.txt1"));
    ASSERT_TRUE(fs::exists(BuildDirectory() + "/fileTraceListenerRollover.txt2"));
    ASSERT_FALSE(fs::exists(BuildDirectory() + "/fileTraceListenerRollover.txt3"));
    ASSERT_TRUE(ValidateContents(BuildDirectory() + "/fileTraceListenerRollover.txt", "test"));
    ASSERT_TRUE(ValidateContents(BuildDirectory() + "/fileTraceListenerRollover.txt1", "world"));
    ASSERT_TRUE(ValidateContents(BuildDirectory() + "/fileTraceListenerRollover.txt2", "hello"));

    TraceInfo("data");
    ThreadSleep(250); // should be plenty of time for the file to have been written

    ASSERT_TRUE(fs::exists(BuildDirectory() + "/fileTraceListenerRollover.txt"));
    ASSERT_TRUE(fs::exists(BuildDirectory() + "/fileTraceListenerRollover.txt1"));
    ASSERT_TRUE(fs::exists(BuildDirectory() + "/fileTraceListenerRollover.txt2"));
    ASSERT_TRUE(fs::exists(BuildDirectory() + "/fileTraceListenerRollover.txt3"));
    ASSERT_TRUE(ValidateContents(BuildDirectory() + "/fileTraceListenerRollover.txt", "data"));
    ASSERT_TRUE(ValidateContents(BuildDirectory() + "/fileTraceListenerRollover.txt1", "test"));
    ASSERT_TRUE(ValidateContents(BuildDirectory() + "/fileTraceListenerRollover.txt2", "world"));
    ASSERT_TRUE(ValidateContents(BuildDirectory() + "/fileTraceListenerRollover.txt3", "hello"));

    TraceInfo("pop");
    ThreadSleep(250); // should be plenty of time for the file to have been written

    ASSERT_TRUE(fs::exists(BuildDirectory() + "/fileTraceListenerRollover.txt"));
    ASSERT_TRUE(fs::exists(BuildDirectory() + "/fileTraceListenerRollover.txt1"));
    ASSERT_TRUE(fs::exists(BuildDirectory() + "/fileTraceListenerRollover.txt2"));
    ASSERT_TRUE(fs::exists(BuildDirectory() + "/fileTraceListenerRollover.txt3"));
    ASSERT_TRUE(ValidateContents(BuildDirectory() + "/fileTraceListenerRollover.txt", "pop"));
    ASSERT_TRUE(ValidateContents(BuildDirectory() + "/fileTraceListenerRollover.txt1", "data"));
    ASSERT_TRUE(ValidateContents(BuildDirectory() + "/fileTraceListenerRollover.txt2", "test"));
    ASSERT_TRUE(ValidateContents(BuildDirectory() + "/fileTraceListenerRollover.txt3", "world"));
}