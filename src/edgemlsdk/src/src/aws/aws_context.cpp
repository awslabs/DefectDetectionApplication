#include <aws/core/Aws.h>

#include <Panorama/aws.h>
#include <Panorama/comptr.h>
#include <Panorama/flowcontrol.h>

using namespace Panorama;

static IAwsContext* instance;
static std::mutex mtx;
static Aws::SDKOptions options;

class AwsContext : public UnknownImpl<IAwsContext>
{
public:
    static HRESULT Instance(IAwsContext** ppObj)
    {
        HRESULT hr = S_OK;
        CHECKNULL(ppObj, E_POINTER);

        {
            std::lock_guard<std::mutex> lk(mtx);
            if(instance == nullptr)
            {
                instance = new (std::nothrow) AwsContext();
                if(instance == nullptr)
                {
                    return E_OUTOFMEMORY;
                }

                TraceInfo("Initializing AWS API");
                Aws::InitAPI(options);
            }
            else
            {
                instance->AddRef();
            }

            *ppObj = instance;
        }

        return hr;
    }

    ~AwsContext()
    {
        std::lock_guard<std::mutex> lk(mtx);
        instance = nullptr;
        TraceInfo("Shutting down AWS API");
        Aws::ShutdownAPI(options);
    }

private:
    AwsContext() = default;
};

DLLAPI HRESULT AwsContext(IAwsContext** ppObj)
{
    return AwsContext::Instance(ppObj);
}
