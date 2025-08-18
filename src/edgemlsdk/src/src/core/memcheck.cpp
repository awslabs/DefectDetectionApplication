#include <Panorama/apidefs.h>
#include <Panorama/trace.h>
#include <mutex>
#include <map>

static std::mutex mtx;
static std::map<void*, std::string> allocated_objects;
static bool memcheck_enabled = false;

DLLAPI void enable_memcheck(bool enabled)
{
    memcheck_enabled = enabled;
}

DLLAPI void com_obj_created(void* pObj, const char* identifier)
{
    if(memcheck_enabled)
    {
        std::lock_guard<std::mutex> lk(mtx);
        if(allocated_objects.find(pObj) == allocated_objects.end())
        {
            allocated_objects[pObj] = identifier;
        }
        else
        {
            TraceWarning("Creating a new object at location (%p) which already exists!!!", pObj);
        }
    }
}

DLLAPI void com_obj_destroyed(void* pObj)
{
    if(memcheck_enabled)
    {
        std::lock_guard<std::mutex> lk(mtx);
        auto iter = allocated_objects.find(pObj);
        if(iter == allocated_objects.end())
        {
            TraceWarning("Removing an object (%p) that does not appear to have been created!!!", pObj);
        }
        else
        {
            allocated_objects.erase(iter);
        }
    }
}

DLLAPI HRESULT memcheck()
{
    if(memcheck_enabled == false)
    {
        return S_OK;
    }

    std::lock_guard<std::mutex> lk(mtx);
    if(allocated_objects.size() == 0)
    {
        return S_OK;
    }

    for(auto iter = allocated_objects.begin(); iter != allocated_objects.end(); iter++)
    {
        TraceInfo("Dangling reference to %s at %p", iter->second.c_str(), iter->first);
    }

    return E_FAIL;
}

