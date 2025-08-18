#include <vector>
#include <string>
#include <fstream>
#include <sstream>
#include <queue>

#include <thread>
#include <condition_variable>
#include <mutex>

#include <Panorama/comobj.h>
#include <Panorama/comptr.h>
#include <Panorama/trace.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/eventing.h>
#include <filesystem_safe.h>
using namespace Panorama;


class FileTraceListener : public UnknownImpl<ITraceListener>
{
public:
    static HRESULT Create(ITraceListener** ppObj, const char* filename, int32_t max_size, int32_t num_backup)
    {
        HRESULT hr = S_OK;
        CHECKNULL(ppObj, E_POINTER);
        *ppObj = nullptr;

        CHECKNULL(filename, E_INVALIDARG);
        CHECKIF(max_size <= 0, E_OUTOFRANGE);
        CHECKIF(num_backup <= 0, E_OUTOFRANGE);

        ComPtr<FileTraceListener> ptr;
        ptr.Attach(new (std::nothrow) FileTraceListener());
        CHECKNULL(ptr, E_OUTOFMEMORY);

        ptr->_filename = filename;
        ptr->_max_size = max_size;
        ptr->_num_backup = num_backup;

        CHECKHR(ptr->Rotate());
        ptr->WriteMessageThread();

        *ppObj = ptr.Detach();
        return S_OK;
    }

    ~FileTraceListener()
    {
        _running = false;
        _waitForMessage.Set();
        if (_writeThread.joinable())
        {
            _writeThread.join();
        }

        _ofstream.close();
    }

    void WriteMessage(TraceLevel level, Timestamp timestamp, int line, const char* file, const char* message) override
    {
        std::stringstream ss;
        ss << timestamp << "\t" << _levels[static_cast<int>(level)] << "\t" << file << ":" << line << "\t" << message;

        std::lock_guard<std::mutex> lk(_writeMtx);
        if (_running)
        {
            _messages.push(ss.str());
            _waitForMessage.Set();
        }
    }

private:
    void WriteMessageThread()
    {
        _current_size = 0;
        _writeThread = std::thread([this]()
            {
                while (_running)
                {
                    _waitForMessage.Wait();

                    while (_messages.empty() == false)
                    {
                        std::lock_guard<std::mutex> lk(_writeMtx);
                        std::string str = _messages.front();
                        _messages.pop();

                        if(_current_size + str.length() > this->_max_size)
                        {
                            TraceVerbose("Rotating log files");
                            if(FAILED(this->Rotate()))
                            {
                                this->_running = false;
                                break;
                            }
                        }

                        _ofstream.write(str.c_str(), str.length());
                        _ofstream.write("\n", 1);
                        _ofstream.flush();

                        _current_size += str.length();

                        if(_messages.empty())
                        {
                            _waitForMessage.Reset();
                        }
                    }
                }
            });
    }

    HRESULT Rotate()
    {
        HRESULT hr = S_OK;

        _current_size = 0;

        // Close the current file
        if(_ofstream.is_open())
        {
            _ofstream.close();
        }

        // delete the oldest file if rolling over will cause it to exceed the max number of backups
        {
            std::string oldest = _filename + std::to_string(_num_backup);
            if(fs::exists(oldest))
            {
                if(fs::remove(oldest) == false)
                {
                    TraceWarning("Could not delete backup log file %s", oldest.c_str());
                }
            }
        }

        // move the current backups to +1 
        for(int32_t idx = _num_backup - 1; idx >= 1; idx--)
        {
            std::string current_name = _filename + std::to_string(idx);
            if(fs::exists(current_name))
            {
                std::string new_name = _filename + std::to_string(idx + 1);
                fs::rename(current_name, new_name);
            }
        }
        
        // move current file to _filename1
        {
            if(fs::exists(_filename))
            {
                std::string new_name = _filename + std::to_string(1);
                fs::rename(_filename, new_name);
            }
        }

        // Open the new file for writing
        _ofstream = std::ofstream(_filename, std::ofstream::out);
        if (_ofstream.is_open() == false)
        {
            TraceError("Could not open %s for writing", _filename.c_str());
            return E_FAIL;
        };

        return hr;
    }

    std::queue<std::string> _messages;

    std::thread _writeThread;
    AutoResetEvent _waitForMessage;

    std::mutex _writeMtx;
    bool _running = true;
    int32_t _num_backup = 0;
    int32_t _max_size = 0;
    int32_t _current_size = 0;
    std::string _filename;

    std::ofstream _ofstream;
    FileTraceListener() = default;
    inline static std::vector<std::string> _levels = { "Error", "Warning", "Info", "Verbose", "Debug" };
};

DLLAPI HRESULT CreateFileTraceListener(ITraceListener** ppObj, const char* filename, int32_t max_size, int32_t num_backup)
{
    return FileTraceListener::Create(ppObj, filename, max_size, num_backup);
}