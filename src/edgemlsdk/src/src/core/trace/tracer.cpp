#include <vector>
#include <mutex>

#include <Panorama/trace.h>
#include <Panorama/comptr.h>
#include <Panorama/flowcontrol.h>
#include <list>

using namespace Panorama;

class TracerImpl
{
public:
    TracerImpl()
    {
    }

    void Trace(TraceLevel level, Timestamp timestamp, int line, const char* file, const char* message)
    {
        std::lock_guard<std::mutex> lk(_mtx);
        if (level <= _level)
        {
            for (auto iter = _listeners.begin(); iter != _listeners.end(); iter++)
            {
                iter->_ptr->WriteMessage(level, timestamp, line, file, message);
            }
        }
    }

    HRESULT AddTraceListener(ITraceListener* listener)
    {
        CHECKNULL(listener, E_INVALIDARG);

        std::lock_guard<std::mutex> lk(_mtx);
        _listeners.push_back(listener);
        return S_OK;
    }

    HRESULT RemoveTraceListener(ITraceListener* listener)
    {
        CHECKNULL(listener, E_INVALIDARG);

        std::lock_guard<std::mutex> lk(_mtx);
        _listeners.remove(listener);
        return S_OK;
    }

    void SetTraceLevel(TraceLevel level)
    {
        std::lock_guard<std::mutex> lk(_mtx);
        _level = level;
    }

    TraceLevel GetTraceLevel()
    {
        std::lock_guard<std::mutex> lk(_mtx);
        return _level;
    }

    
private:
    inline static std::mutex _mtx;

    TraceLevel _level = TraceLevel::Information;
    std::list<ComPtr<ITraceListener>> _listeners;
};

static TracerImpl _instance;

DLLAPI void Trace(TraceLevel level, Timestamp timestamp, int line, const char* file, const char* message)
{
    _instance.Trace(level, timestamp, line, file, message);
}

DLLAPI HRESULT AddTraceListener(ITraceListener* listener)
{
    return _instance.AddTraceListener(listener);
}

DLLAPI HRESULT RemoveTraceListener(ITraceListener* listener)
{
    return _instance.RemoveTraceListener(listener);
}

DLLAPI void SetTraceLevel(TraceLevel level)
{
    _instance.SetTraceLevel(level);
}

DLLAPI TraceLevel GetTraceLevel()
{
    return _instance.GetTraceLevel();
}