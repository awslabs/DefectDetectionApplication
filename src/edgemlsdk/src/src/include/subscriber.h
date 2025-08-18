#ifndef __SUBSCRIBER_H__
#define __SUBSCRIBER_H__
#include <map>
#include <mutex>

#include <Panorama/apidefs.h>
#include <Panorama/chrono.h>

namespace Panorama
{
    template<typename ... Args>
    class Subscriber
    {
    public:
        int64_t Register(void* pUserData, void(*cb)(void* pUserData, Args...))
        {
            std::lock_guard<std::mutex> lk(_mtx);
            int64_t token = NowAsTimestamp();
            _callbacks[token].first = pUserData;
            _callbacks[token].second = cb;
            return token;
        }

        void Unregister(int64_t token)
        {
            std::lock_guard<std::mutex> lk(_mtx);
            if (_callbacks.find(token) != _callbacks.end())
            {
                _callbacks.erase(token);
            }
        }

        void Invoke(Args...param)
        {
            // Possible for Register/Unregsiter to be called while invoking.
            // Instead of increasing change for deadlock by locking until all invocation is complete
            // make shallow copy of _callbacks and iterate through shallow copy
            std::lock_guard<std::mutex> lk(_mtx);
            std::map<int64_t, std::pair<void*, void(*)(void* pUserData, Args...)>> shallowCopy(_callbacks);
            lk.~lock_guard();

            for (auto const& it : shallowCopy)
            {
                it.second.second(it.second.first, param...);
            }
        }

    private:
        std::map<int64_t, std::pair<void*, void(*)(void* pUserData, Args...)>> _callbacks;
        std::mutex _mtx;
    };

    template<typename Ret, typename ... Args>
    class Callback
    {
    public:
        void Register(void* pUserData, Ret(*cb)(void* pUserData, Args...))
        {
            _callback = cb;
            _pUserData = pUserData;
        }

        Ret Invoke(Args...param)
        {
            if (_callback != nullptr)
            {
                return _callback(_pUserData, param...);
            }

            return Ret();
        }

        void Unregister()
        {
            _callback = nullptr;
        }

    private:
        Ret(*_callback)(void* pUserData, Args...);
        void* _pUserData;
    };
}

#endif