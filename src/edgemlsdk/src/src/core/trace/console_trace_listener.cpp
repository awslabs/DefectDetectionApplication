#include <vector>
#include <string>
#include <sstream>
#include <iostream>
#include <iomanip>

#include <Panorama/comobj.h>
#include <Panorama/comptr.h>
#include <Panorama/trace.h>
#include <Panorama/flowcontrol.h>

using namespace Panorama;

#define MESSAGE_COL 70
class ConsoleTraceListener : public UnknownImpl<ITraceListener>
{
public:
    static HRESULT Create(ITraceListener** ppObj)
    {
        ComPtr<ConsoleTraceListener> ptr;
        ptr.Attach(new (std::nothrow) ConsoleTraceListener());
        CHECKNULL(ptr, E_OUTOFMEMORY);

        *ppObj = ptr.Detach();
        return S_OK;
    }

    void WriteMessage(TraceLevel level, Timestamp timestamp, int line, const char* file, const char* message) override
    {
        std::stringstream ss;

        ss << "(" << timestamp << ")" << "[" << _levels[static_cast<int>(level)].c_str() << "@" << file << ":" << line << "]";
        int fill = MESSAGE_COL - static_cast<int32_t>(ss.str().length()) < 0 ? 0 : MESSAGE_COL - ss.str().length();

        std::stringstream padding;
        padding << std::setw(fill) << ' ';

        std::cout << ss.str() << padding.str() << message << "\n";
    }

private:
    ConsoleTraceListener() = default;
    inline static std::vector<std::string> _levels = { "Error", "Warning", "Info", "Verbose", "Debug"};
};

DLLAPI HRESULT CreateConsoleTraceListener(ITraceListener** ppObj)
{
	return ConsoleTraceListener::Create(ppObj);
}