#ifndef __TRACE_H__
#define __TRACE_H__

#include <stdarg.h>
#include <string>

#include <Panorama/apidefs.h>
#include <Panorama/unknown.h>
#include <Panorama/chrono.h>

namespace Panorama
{
    /// @brief Enumeration for levels of tracing
    enum class TraceLevel
    {
        /// @brief Traces all fatal errors.  Errors that do not allow the continuation of a procedure.  Program can still  recover if the error conditions are handled.
        Error = 0,

        /// @brief  Traces all warnings.  Non fatal errors/conditions that allow the continuation of a procedure, but the developer may want to be informed about it.
        Warning,

        /// @brief Traces all informational messages.  Events that happen once or infrequently, gives high level tracing of the progress of a procedure.
        Information,

        /// @brief Traces all verbose messages.  Events that happen repeatedly, gives more detailed information about the progroess of a procedure but may pollute the logs with redudant and unecessary information.
        Verbose,

        /// @brief Traces all debug messages.  Traces all reference count changes and HttpCall request/response headers and bodies as well as any additional information that may be considered useful for debugging issues.
        Debug
    };

    DEF_INTERFACE(ITraceListener, "{D7AFD57A-9A91-4E2E-831C-4B61D17467AD}", IUnknownAlias)
    {
        /// @brief Instructs the trace listener that a new message is available
        /// @param level The level of the trace 
        /// @param timestamp The timestamp of the message.  Resolution is 10^-7 seconds since epoch (Jan 1st 1970) 
        /// @param line The line number in the file from which the trace statement originated
        /// @param file The full file path from which the trace statement originated
        /// @param message The message
        virtual void WriteMessage(TraceLevel level, Timestamp timestamp, int line, const char* file, const char* message) = 0;
    };

    /* \cond C-Style Methods for Tracing.  Use Tracer API */
    // App users should static methods in Tracer instead of these directly
    DLLAPI void Trace(TraceLevel level, Timestamp timestamp, int line, const char* file, const char* message);
    DLLAPI HRESULT AddTraceListener(ITraceListener* listener);
    DLLAPI HRESULT RemoveTraceListener(ITraceListener* listener);
    DLLAPI void SetTraceLevel(TraceLevel level);
    DLLAPI TraceLevel GetTraceLevel();
    DLLAPI HRESULT CreateConsoleTraceListener(ITraceListener** ppObj);
    DLLAPI HRESULT CreateFileTraceListener(ITraceListener** ppObj, const char* filename, int32_t max_size, int32_t num_backup);
    DLLAPI HRESULT CreateHttpTraceListener(ITraceListener** ppObj, const char* ip, int32_t port);
    /* \endcond */

    /// @brief API for the Panorama tracing framework.
    class Tracer
    {
    public:
        /// @brief Sends a message to all trace listeners that have been added with a call to AddTraceListener
        /// @param level The level of the trace 
        /// @param timestamp The timestamp of the message.  Resolution is 10^-7 seconds since epoch (Jan 1st 1970) 
        /// @param line The line number in the file from which the trace statement originated
        /// @param file The full file path from which the trace statement originated
        /// @param fmt The format of the message
        /// @param ... additional arguments (see printf)
        static void Trace(TraceLevel level, Timestamp timestamp, int line, const char* file, const char* fmt, ...)
        {
            std::string s{};
            va_list args, args2;
            va_start(args, fmt);

            // Compute the size 
            va_copy(args2, args);
            s.resize(vsnprintf(nullptr, 0, fmt, args2) + 1);
            va_end(args2);

            // print to s and remove trailing \0
            vsprintf(s.data(), fmt, args);

            va_end(args);
            s.pop_back();

            Panorama::Trace(level, timestamp, line, file, s.c_str());
        }

        /// @brief Adds a trace listener to recieve trace events
        /// @param listener The listener to add
        /// @return 
        ///     S_OK: The listener was added <br>
        ///     E_INVALIDARG: Listener is null
        static HRESULT AddTraceListener(ITraceListener* listener)
        {
            return Panorama::AddTraceListener(listener);
        }

        /// @brief Removes a trace listener from recieving trace events
        /// @param listener The listener to remove
        /// @return 
        ///     S_OK: The listener was added <br>
        ///     E_INVALIDARG: Listener is null
        static HRESULT RemoveTraceListener(ITraceListener* listener)
        {
            return Panorama::RemoveTraceListener(listener);
        }

        /// @brief Sets the filter of the trace level.  No trace listener will be called with traces that have levels higher than the filter.
        /// @param level The level of the filter.
        static void SetTraceLevel(TraceLevel level)
        {
            Panorama::SetTraceLevel(level);
        }

        /// @brief Gets the current trace level
        /// @return The current trace level
        static TraceLevel GetTraceLevel()
        {
            return Panorama::GetTraceLevel();
        }

        /// @brief Creates a trace listener that will write all messages to stdout
        /// @param ppObj Address of the ITraceListener* to hold the created object
        /// @return 
        ///     S_OK: The object was created <br>
        ///     E_POINTER: ppObj is null <br>
        ///     E_OUTOFMEMORY: Could not allocate the object
        static HRESULT CreateConsoleTraceListener(ITraceListener** ppObj)
        {
            return Panorama::CreateConsoleTraceListener(ppObj);
        }

        /// @brief Creates a trace listener that will write all messages to a file
        /// @param ppObj Address of the ITraceListener* to hold the created object
        /// @param filename The path of the file to write too
        /// @param max_size The maximum size (in bytes) of the current log file
        /// @param num_backup The number of files that will be retained after they overflow
        /// @return 
        ///     S_OK: The object was created <br>
        ///     E_POINTER: ppObj is null <br>
        ///     E_OUTOFMEMORY: Could not allocate the object
        ///     E_INVALIDARG: Filename is null
        ///     E_FAIL: Could not open the file for writing
        static HRESULT CreateFileTraceListener(ITraceListener** ppObj, const char* filename, int32_t max_size, int32_t num_backup)
        {
            return Panorama::CreateFileTraceListener(ppObj, filename, max_size, num_backup);
        }

        /// @brief Creates a trace listener that sends messages over http to http://<ip>:<port>/trace as a POST message
        /// @param ppObj Pointer to the created ITraceListener*
        /// @param ip Ip address of the RESTful server
        /// @param port Port over which to communicate
        /// @return S_OK (0) on Success.  Error code otherwise
        static HRESULT CreateHttpTraceListener(ITraceListener** ppObj, const char* ip, int32_t port)
        {
            return Panorama::CreateHttpTraceListener(ppObj, ip, port);
        }
    };

#define __FILENAME__ (strrchr(__FILE__, '/') ? strrchr(__FILE__, '/') + 1 : __FILE__)
#define TraceError(FMT, ...) Panorama::Tracer::Trace(Panorama::TraceLevel::Error, Panorama::NowAsTimestamp(), __LINE__, __FILENAME__, FMT, ##__VA_ARGS__)
#define TraceWarning(FMT, ...) Panorama::Tracer::Trace(Panorama::TraceLevel::Warning, Panorama::NowAsTimestamp(), __LINE__, __FILENAME__, FMT, ##__VA_ARGS__)
#define TraceInfo(FMT, ...) Panorama::Tracer::Trace(Panorama::TraceLevel::Information, Panorama::NowAsTimestamp(), __LINE__, __FILENAME__, FMT, ##__VA_ARGS__)
#define TraceVerbose(FMT, ...) Panorama::Tracer::Trace(Panorama::TraceLevel::Verbose, Panorama::NowAsTimestamp(), __LINE__, __FILENAME__, FMT, ##__VA_ARGS__)
#define TraceDebug(FMT, ...) Panorama::Tracer::Trace(Panorama::TraceLevel::Debug, Panorama::NowAsTimestamp(), __LINE__, __FILENAME__, FMT, ##__VA_ARGS__)
#define TraceLog(LEVEL, FMT, ...) Panorama::Tracer::Trace(LEVEL, Panorama::NowAsTimestamp(), __LINE__, __FILENAME__, FMT, ##__VA_ARGS__)
#define ADD_CONSOLE_TRACE { ITraceListener* console; CreateConsoleTraceListener(&console); Panorama::AddTraceListener(console); }
}

#endif
