#include <vector>
#include <string>
#include <queue>
#include <thread>
#include <mutex>
#include <atomic>

#include <nlohmann/json.hpp>

#include <Panorama/comobj.h>
#include <Panorama/comptr.h>
#include <Panorama/trace.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/eventing.h>

#include <core/rest/rest.h>

using namespace Panorama;

class HttpTraceListener : public UnknownImpl<ITraceListener>
{
public:
    static HRESULT Create(ITraceListener** ppObj, const char* ip, int32_t port)
    {
        COM_FACTORY(HttpTraceListener, Initialize(ip, port));
    }

    ~HttpTraceListener()
    {
        COM_DTOR(HttpTraceListener);

        _running = false;
        _waitForMessage.Set();
        if (_writeThread.joinable())
        {
            _writeThread.join();
        }

        COM_DTOR_FIN(HttpTraceListener);
    }

    void WriteMessage(TraceLevel level, Timestamp timestamp, int line, const char* file, const char* message) override
    {
        nlohmann::json msg;
        msg["Level"] = static_cast<int32_t>(level);
        msg["Timestamp"] = timestamp;
        msg["Line"] = line;
        msg["File"] = std::string(file);
        msg["Message"] = std::string(message);

        std::lock_guard<std::mutex> lk(_writeMtx);
        if (_running)
        {
            _messages.push(msg);
            _waitForMessage.Set();
        }
    }

private:
    HRESULT Initialize(const char* ip, int32_t port)
    {
        HRESULT hr = S_OK;
        CHECKNULL_OR_EMPTY(ip, E_INVALIDARG);
        CHECKIF(port <= 0, E_INVALIDARG);

        CHECKHR(CreateRESTClient(_client.AddressOf(), ip, port, true));

        _writeThread = std::thread([this]()
        {
            WriteMessageInternal();
        });

        return hr;
    }

    void WriteMessageInternal()
    {
        while (_running)
        {
            _waitForMessage.Wait();

            while (true)
            {
                nlohmann::json msg;

                {
                    std::lock_guard<std::mutex> lk(_writeMtx);
                    if(_messages.empty())
                    {
                        break;
                    }

                    msg = _messages.front();
                    _messages.pop();
                }

                std::string serialized = msg.dump();
                ComPtr<IBuffer> buffer;
                ComPtr<IHttpRequest> request;

                if(SUCCEEDED(Buffer::Create(buffer.AddressOf(), serialized.length())) 
                    && SUCCEEDED(CreateHttpRequest(request.AddressOf())))
                {
                    memcpy(buffer->Data(), serialized.c_str(), serialized.length());
                    request->AddHeader("Content-Type", "application/json");
                    request->SetMethod(MethodType::POST);
                    if( SUCCEEDED(request->SetPath("trace")) && SUCCEEDED(request->AddBody(buffer)))
                        {
                            ComPtr<IHttpResponse> response;
                            if(FAILED(_client->MakeRequest(response.AddressOf(), request)))
                            {
                                // Failed to make request, just end, likely becuase endpoint isn't listening
                                // Down the road can perhaps add some retry logic
                                // Keep it simple for now
                                TraceInfo("Failed to send message, stopping http trace listener");
                                _running = false;
                                break;
                            }
                        }
                }

                std::lock_guard<std::mutex> lk(_writeMtx);
                if(_messages.empty())
                {
                    _waitForMessage.Reset();
                }
            }
        }
    }

    ComPtr<IRestAPI> _client;
    std::queue<nlohmann::json> _messages;
    std::thread _writeThread;
    AutoResetEvent _waitForMessage;
    std::mutex _writeMtx;
    std::atomic<bool> _running = true;

    HttpTraceListener() = default;
};

DLLAPI HRESULT CreateHttpTraceListener(ITraceListener** ppObj, const char* ip, int32_t port)
{
    return HttpTraceListener::Create(ppObj, ip, port);
}