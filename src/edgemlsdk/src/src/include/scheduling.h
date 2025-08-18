#ifndef __SCHEDULING_HPP__
#define __SCHEDULING_HPP__

#include <functional>
#include <thread>
#include <mutex>
#include <queue>
#include <future>
#include <Panorama/trace.h>
#include <Panorama/eventing.h>

#define JOB std::pair<T, std::function<void(HRESULT)>>
// Default calculated based on max files written per DDA inference.
#define DEFAULT_MT_JOB_QUEUE_WORKERS 4
#define DEFAULT_MT_JOB_QUEUE_NAME "MTJobQueue"

template<typename T>
class JobQueue
{
public:
    JobQueue() : 
        _running(false),
        _paused(true),
        _running_job(true)
    {
    }

    ~JobQueue()
    {
        Stop();
    }

    void SetProcessor(std::function<HRESULT(T)> processor)
    {
        _cb = std::move(processor);
    }

    void Start()
    {
        {
            std::lock_guard<std::mutex> lk(_mtx);
            if(_running)
            {
                return;
            }

            _running = true;
        }

        _thread = std::thread([&]()
        {
            _job_thread_id = std::this_thread::get_id();
            while(_running)
            {
                _dataAvailable.Wait();

                {
                    std::lock_guard<std::mutex> pause_lk(_pause_mtx);
                    _paused.Wait();
                    _running_job.Reset();
                }

                if(_running == false)
                {
                    break;
                }

                JOB job;
                {
                    std::lock_guard<std::mutex> lk(_mtx);
                    job = _queue.front();
                    _queue.pop();

                    if(_queue.empty())
                    {
                        _dataAvailable.Reset();
                    }
                }

                // Call processing job
                HRESULT hr = _cb(job.first);

                // Call complete job
                if(job.second)
                {
                    job.second(hr);
                }

                _running_job.Set();
            }
        });
    }

    void Pause()
    {
        std::lock_guard<std::mutex> pause_lk(_pause_mtx);
        _paused.Reset();
        _running_job.Wait();
    }

    void Stop()
    {
        {
            std::lock_guard<std::mutex> lk(_mtx);
            if(_running == false)
            {
                return;
            }

            _running = false;
            _dataAvailable.Set();
            _paused.Set();
        }

        if(_thread.joinable())
        {
            _thread.join();
        }
    }

    void Enqueue(const T& data, std::function<void(HRESULT)> processed=nullptr)
    {
        std::lock_guard<std::mutex> lk(_mtx);
        JOB job;
        job.first = data;
        job.second = std::move(processed);
        _queue.push(job);
        _dataAvailable.Set();
    }

    void Clear()
    {
        std::lock_guard<std::mutex> lk(_mtx);
        std::queue<T> empty;
        _queue.swap(empty);
        _dataAvailable.Reset();
    }

    

    void Resume()
    {
        _paused.Set();
    }

private:
    std::thread::id _job_thread_id;
    std::thread _thread;
    ManualResetEvent _dataAvailable;
    ManualResetEvent _paused;
    ManualResetEvent _running_job;
    bool _running;
    std::mutex _mtx;
    std::mutex _pause_mtx;
    std::queue<JOB> _queue;
    std::function<HRESULT(T)> _cb;
};

template<typename T>
class MultiThreadedJobQueue
{
public:
    MultiThreadedJobQueue() :
        _num_workers(DEFAULT_MT_JOB_QUEUE_WORKERS),
        _name(DEFAULT_MT_JOB_QUEUE_NAME),
        _stop(false)
    {
    }

    ~MultiThreadedJobQueue()
    {
        Stop();
    }
    void SetProcessor(std::function<HRESULT(T)> processor) {
        std::unique_lock<std::mutex> lock(_queue_mutex);
        _processor = [processor=std::move(processor)](JOB job) -> void {
            HRESULT hr = processor(job.first);
            auto _cb = std::move(job.second);
            if(_cb) {
                _cb(hr);
            }
        };
    }
    // This two methods , need to be called before Start().
    void SetNumWorkers(uint8_t num_workers) {
        std::unique_lock<std::mutex> lock(_queue_mutex);
        _num_workers = num_workers;
    }
    void SetName(const std::string& name) {
        std::unique_lock<std::mutex> lock(_queue_mutex);
        _name = name;
    }
    void Start() {
        TraceInfo("Starting Multithreaded Job Queue %s", _name.c_str());

        _thread = std::thread([&](){
            while (true) {
                std::unique_lock<std::mutex> lock(_queue_mutex);
                _condition.wait(lock, [this] { return _stop || !_queue.empty(); });
                if (_stop && _queue.empty()) {
                        break;
                }
                if(_workers.size() >= _num_workers) {
                    TraceVerbose("Queue %s is full , waiting for threads to be avaialble to execute.", _name.c_str());
                    // wait for front to finish and pop.
                    if(_workers.front().wait_for(std::chrono::seconds(0)) == std::future_status::ready) {
                        _workers.pop();
                    }
                    continue;
                }
                TraceInfo("started job from Multithreaded job queue %s", _name.c_str());
                _workers.emplace(std::async(std::launch::async, _processor,std::move(_queue.front())));
                _queue.pop();
            }
        });
    }
    void Stop() {
        {
            std::unique_lock<std::mutex> lock(_queue_mutex);
            if (_stop) {
                return;
            }
            _stop = true;
            while (!_workers.empty()) {
                _workers.front().wait();
                _workers.pop();
            }
        }
        _condition.notify_all();
        if (_thread.joinable()) {
            _thread.join();
        }
        TraceInfo("Stopped Multithreaded Job Queue %s", _name.c_str());
    }
    void Enqueue(const T& data, std::function<void(HRESULT)> processed=nullptr) {
        {
            std::unique_lock<std::mutex> lock(_queue_mutex);
            JOB job;
            job.first = data;
            job.second = std::move(processed);
            _queue.push(job);
        }

        _condition.notify_one();
        TraceInfo("Queued job to Multithreaded job queue %s", _name.c_str());

    }
    uint8_t _num_workers;
    std::thread _thread;
    std::queue<JOB> _queue;
    std::mutex _queue_mutex;
    std::condition_variable _condition;
    bool _stop;
    std::function<void(JOB)> _processor;
    std::queue<std::future<void>> _workers;
    std::string _name;
};
#endif