#ifndef __EVENTING_H__
#define __EVENTING_H__

#include <mutex>
#include <condition_variable>
#include <chrono>

using namespace std::chrono;

class ManualResetEvent
{
public:
    ManualResetEvent(bool set = false) 
        : _set(set)
    {
    }

    ~ManualResetEvent()
    {
        Set();
    }

    void Set()
    {
        std::lock_guard<std::mutex> lk(_mtx);
        _set = true;
        _cv.notify_all();
    }

    void Reset()
    {
        std::lock_guard<std::mutex> lk(_mtx);
        _set = false;
    }

    void Wait()
    {
        std::unique_lock<std::mutex> lk(_mtx);
        _cv.wait(lk, [this]()
            {
                bool ret = _set == true;
                if (ret)
                {
                    OnSignaled();
                }

                return ret;
            });
    }

    bool WaitFor(int32_t ms)
    {
        std::unique_lock<std::mutex> lk(_mtx);
        return _cv.wait_for(lk, std::chrono::duration<int32_t, std::ratio<1, 1000>>(ms), [this]()
            {
                bool ret = _set == true;
                if (ret)
                {
                    OnSignaled();
                }

                return ret;
            });
    }

protected:
    virtual void OnSignaled()
    {
    }

    std::mutex _mtx;
    bool _set;
    std::condition_variable _cv;
};

class AutoResetEvent : public ManualResetEvent
{
public:
    AutoResetEvent(bool set = false)
    {
        _set = set;
    }

    void OnSignaled() override
    {
        _set = false;
    }
};

#endif