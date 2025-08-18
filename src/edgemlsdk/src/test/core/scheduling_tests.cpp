#include <thread>
#include <vector>

#include <gtest/gtest.h>
#include <Panorama/apidefs.h>
#include <Panorama/eventing.h>
#include <scheduling.h>
#include <Panorama/trace.h>

#include "../TestUtils.h"

using namespace std;

TEST(SchedulingTests, JobQueueTests)
{
    JobQueue<int> queue;

    std::vector<ManualResetEvent> set(3);
    std::vector<ManualResetEvent> complete(3);

    queue.SetProcessor([&](int x)
    {
        set[x].Set();
        return S_OK;
    });

    queue.Start();

    for(int i = 0; i < set.size(); i++)
    {
        queue.Enqueue(i, [i, &complete](HRESULT hr)
        {
            complete[i].Set();
        });
    }

    for(int i = 0; i < set.size(); i++)
    {
        ASSERT_TRUE(set[i].WaitFor(3000));
        ASSERT_TRUE(complete[i].WaitFor(3000));
    }
}

TEST(SchedulingTests, MultiThreadedJobQueueTests)
{
    {
        TraceInfo("MultiThreadedJobQueueTests");
        // More jobs than workers, update vectors.
        MultiThreadedJobQueue<int> queue;
        queue.SetNumWorkers(3);
        std::vector<bool> update(4,false);
        std::vector<bool> complete(4,false);

        TraceInfo("MultiThreadedJobQueueTests Set procesor");

        queue.SetProcessor([&](int x)
        {
            TraceInfo("Processor start");
            update[x] = true;
            TraceInfo("Processor end");
            return S_OK;
        });
        TraceInfo("Start");
        queue.Start();
        TraceInfo("Enqueue");

        for(int i = 0; i < update.size(); i++)
        {
            queue.Enqueue(i, [i, &complete](HRESULT hr)
            {
            complete[i] = true;
            });
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(2000));
        queue.Stop();
        for(int i = 0; i < update.size(); i++)
        {
            ASSERT_TRUE(update[i]);
            ASSERT_TRUE(complete[i]);
        }
    }

    {
        // Atomic counter, lower jobs than max workers.
        MultiThreadedJobQueue<int> queue;
        queue.SetNumWorkers(5);
        std::atomic<uint8_t> counter = 0;


        queue.SetProcessor([&](int x)
        {
            counter += x;
            return S_OK;
        });

        queue.Start();

        for(int i = 0; i < 4; i++)
        {
            queue.Enqueue(i);
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(1000));
        ASSERT_TRUE(counter == 6);
    }
    {
        // different time taking processor, single threaded queue.
        MultiThreadedJobQueue<uint8_t> queue;
        queue.SetNumWorkers(1);

        std::atomic<uint8_t> counter = 0;


        queue.SetProcessor([&](uint8_t x)
        {
            if (x > 0){
                std::this_thread::sleep_for(std::chrono::seconds(2));
            }
            counter += 1;

            return S_OK;
        });
        queue.Start();
        for(uint8_t i = 0; i < 2; i++)
        {
            queue.Enqueue(i);
        }
        std::this_thread::sleep_for(std::chrono::seconds(1));
        ASSERT_TRUE(counter == 1);
        std::this_thread::sleep_for(std::chrono::seconds(3));
        ASSERT_TRUE(counter == 2);
        queue.Stop();
    }
    {
        // serial vs concurrent execution.
        MultiThreadedJobQueue<int> queue;
        queue.SetNumWorkers(4);
        auto fn = [&](int x)
        {
            std::this_thread::sleep_for(std::chrono::seconds(x));
            return S_OK;
        };
        queue.SetProcessor(fn);
        // measure serial execution time
        auto start = std::chrono::high_resolution_clock::now();
        for (int i=0; i<4;i++)
        {
            fn(2);
        }
        auto end = std::chrono::high_resolution_clock::now();
        auto duration_serial = std::chrono::duration_cast<std::chrono::microseconds>(end - start).count();
        queue.Start();
        start = std::chrono::high_resolution_clock::now();
        for(int i = 0; i < 4; i++)
        {
            queue.Enqueue(2);
        }
        queue.Stop();
        end = std::chrono::high_resolution_clock::now();
        auto duration_multi_concurrent = std::chrono::duration_cast<std::chrono::microseconds>(end - start).count();
        ASSERT_TRUE(duration_multi_concurrent < duration_serial);
    }
}