#include <Panorama/apidefs.h>

DLLAPI const char* GetVersionString()
{
    return __SDK_VERSION__;
}

DLLAPI const char* GetMajorMinorVersionString()
{
    return __SDK_MAJOR_MINOR_VERSION__;
}