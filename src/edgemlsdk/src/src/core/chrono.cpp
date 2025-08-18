#include <chrono>
#include <thread>
#include <sstream>

#include <Panorama/chrono.h>

using namespace Panorama;
using namespace std::chrono;
using decimicroseconds = duration<int64_t, std::ratio<1, 10000000>>;

DLLAPI Timestamp NowAsTimestamp()
{
    Timestamp now = duration_cast<decimicroseconds>(system_clock::now().time_since_epoch()).count();
    return now;
}

DLLAPI Timestamp TimestampToMilliseconds(Timestamp timestamp)
{
    return duration_cast<milliseconds>(decimicroseconds(timestamp)).count();
}

DLLAPI void ThreadSleep(int32_t milliseconds)
{
    std::this_thread::sleep_for(std::chrono::milliseconds(milliseconds));
}