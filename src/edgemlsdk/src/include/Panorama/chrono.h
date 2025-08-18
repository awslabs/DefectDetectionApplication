#ifndef __CHRONO_H__
#define __CHRONO_H__
#include <Panorama/apidefs.h>

typedef int64_t Timestamp;

namespace Panorama
{
    DLLAPI Timestamp NowAsTimestamp();
    DLLAPI Timestamp TimestampToMilliseconds(Timestamp timestamp);
    DLLAPI void ThreadSleep(int32_t milliseconds);

    class Chrono
    {
        static Timestamp NowAsTimestamp()
        {
            return Panorama::NowAsTimestamp();
        }

        static Timestamp TimestampToMilliseconds(Timestamp timestamp)
        {
            return Panorama::TimestampToMilliseconds(timestamp);
        }

        static void ThreadSleep(int32_t milliseconds)
        {
            Panorama::ThreadSleep(milliseconds);
        }
    };

}
#endif