#include <sstream>
#include <curl/curl.h>
#include <vector>

#include <Panorama/comptr.h>
#include <Panorama/flowcontrol.h>

#include "rest.h"

namespace Panorama
{
    struct memory {
        uint8_t* data;
        size_t size;
    };

    struct Response
    {
        long statusCode;
        memory body;
    };

    static size_t WriteCallback(void* data, size_t size, size_t nmemb, void* userp)
    {
        size_t realsize = size * nmemb;
        struct memory* mem = (struct memory*)userp;

        uint8_t* ptr = static_cast<uint8_t*>(realloc(mem->data, mem->size + realsize + 1));
        if (ptr == NULL)
        {
            return 0;  /* out of memory! */
        }

        mem->data = ptr;
        memcpy(&(mem->data[mem->size]), data, realsize);
        mem->size += realsize;
        mem->data[mem->size] = 0;

        return realsize;
    }

    static size_t ReadCallback(char* dest, size_t size, size_t nmemb, void* userp)
    {
        memory* mem = (memory*)userp;
        size_t buffer_size = size * nmemb;

        if (mem->size) 
        {
            /* copy as much as possible from the source to the destination */
            size_t copy_this_much = mem->size;
            if (copy_this_much > buffer_size)
            {
                copy_this_much = buffer_size;
            }
                
            memcpy(dest, mem->data, copy_this_much);

            mem->data += copy_this_much;
            mem->size -= copy_this_much;
            return copy_this_much; /* we copied this many bytes */
        }

        return 0; /* no more data left to deliver */
    }

    class SafeCURL : public UnknownImpl<IUnknownAlias>
    {
    public:
        static HRESULT Create(SafeCURL** ppObj)
        {
            HRESULT hr = S_OK;
            CHECKNULL(ppObj, E_POINTER);
            *ppObj = nullptr;

            ComPtr<SafeCURL> ptr;
            ptr.Attach(new (std::nothrow) SafeCURL());
            CHECKNULL(ptr, E_OUTOFMEMORY);

            ptr->_handle = curl_easy_init();
            CHECKNULL_MSG(ptr->_handle, E_FAIL, "Failed to create cURL handle");

            *ppObj = ptr.Detach();
            return hr;
        }

        ~SafeCURL()
        {
            if (_handle != nullptr)
            {
                curl_easy_cleanup(_handle);
            }
        }

        CURL* Handle()
        {
            return _handle;
        }

    private:
        CURL* _handle = nullptr;
    };

    class CurlImpl : public UnknownImpl<IRestAPI>
    {
    public:
        static HRESULT Create(IRestAPI** ppObj, const char* ip, int port, bool silent)
        {
            HRESULT hr = S_OK;
            CHECKNULL(ppObj, E_POINTER);
            *ppObj = nullptr;

            ComPtr<CurlImpl> ptr;
            ptr.Attach(new (std::nothrow) CurlImpl());
            CHECKNULL(ptr, E_OUTOFMEMORY);

            std::stringstream url;
            url << ip;
            if (port > 0)
            {
                url << ":" << port;
            }
            ptr->_baseAddr = url.str();
            ptr->_silent = silent;

            *ppObj = ptr.Detach();
            return hr;
        }

        HRESULT MakeRequest(IHttpResponse** ppObj, IHttpRequest* request) override
        {
            HRESULT hr = S_OK;
            static int32_t callCounter = 0;
            int32_t callCount = callCounter;
            callCounter++;

            int32_t maxAttempt = 3;

            if (_silent == false)
            {
                TraceDebug("Making HTTP call [%d]: %s", callCount, request->ToString());
            }

            // Get the headers as a vector
            std::vector<std::string> headers;
            request->IterateHeaders([](const char* key, const char* value, void* pUserData)
                {
                    std::stringstream ss;
                    ss << key << ":" << value;
                    reinterpret_cast<std::vector<std::string>*>(pUserData)->push_back(ss.str());
                }, &headers);

            // Get the body
            ComPtr<IBuffer> body;
            request->GetBody(body.AddressOf());

            for (int32_t attempt = 0; attempt < maxAttempt; attempt++)
            {
                HRESULT request_hr = S_OK;
                Response RESTResponse = { 0, 0 };
                request_hr = MakeRequestInternal(&RESTResponse, request->GetPath(), request->GetMethod(), headers, body ? body->Data() : nullptr, body ? body->Size() : 0, callCount);

                ComPtr<IBuffer> buffer;
                CHECKHR(CreateBuffer(buffer.AddressOf(), static_cast<int32_t>(RESTResponse.body.size) + 1));

                buffer->Data()[RESTResponse.body.size] = '\0';
                if (RESTResponse.body.data != nullptr)
                {
                    memcpy(buffer->Data(), RESTResponse.body.data, RESTResponse.body.size);
                    free(RESTResponse.body.data);
                }

                ComPtr<IHttpResponse> response;
                CHECKHR(CreateHttpResponse(response.AddressOf(), static_cast<Panorama::HttpStatusCode>(RESTResponse.statusCode), buffer));

                if (_silent == false)
                {
                    TraceDebug("HTTP response [%d] (code: %d): %s", callCount, request_hr, response->ToString());
                }

                if (SUCCEEDED(request_hr))
                {
                    *ppObj = response.Detach();
                    break;
                }

                if(_silent == false)
                {
                    TraceWarning("HTTP call failed [%d], retrying (%d/%d)", callCount, attempt + 1, maxAttempt);
                }

                ThreadSleep(50);

                hr = request_hr;
            }

            return hr;
        }

    private:
        std::string _baseAddr;
        bool _silent = true;

        HRESULT MakeRequestInternal(Response* response, const char* url, MethodType method, std::vector<std::string> headers, uint8_t* data, int32_t length, int32_t callCount)
        {
            HRESULT hr = S_OK;
            
            ComPtr<SafeCURL> curl;
            CHECKHR(SafeCURL::Create(curl.AddressOf()));

            std::stringstream fullUrl;
            fullUrl << _baseAddr << "/" << url;
            CHECKIF(curl_easy_setopt(curl->Handle(), CURLOPT_URL, fullUrl.str().c_str()) != CURLE_OK, E_FAIL);

            switch (method)
            {
            case MethodType::GET:
                break;
            case MethodType::POST:
                CHECKIF(curl_easy_setopt(curl->Handle(), CURLOPT_POST, 1) != CURLE_OK, E_FAIL);
                break;
            default:
                if(_silent == false)
                {
                    TraceError("Unknown method type");
                }

                return E_INVALIDARG;
            }

            CHECKIF(curl_easy_setopt(curl->Handle(), CURLOPT_WRITEFUNCTION, WriteCallback) != CURLE_OK, E_FAIL);
            CHECKIF(curl_easy_setopt(curl->Handle(), CURLOPT_WRITEDATA, &(response->body)) != CURLE_OK, E_FAIL);

            // Add Body
            memory readData;
            if (data != nullptr)
            {
                readData.data = data;
                readData.size = length;

                CHECKIF(curl_easy_setopt(curl->Handle(), CURLOPT_READDATA, &readData) != CURLE_OK, E_FAIL);
                CHECKIF(curl_easy_setopt(curl->Handle(), CURLOPT_READFUNCTION, ReadCallback) != CURLE_OK, E_FAIL);
                CHECKIF(curl_easy_setopt(curl->Handle(), CURLOPT_POSTFIELDSIZE, (long)length) != CURLE_OK, E_FAIL);
            }

            // Add Headers
            curl_slist* list = NULL;
            for (int32_t idx = 0; idx < headers.size(); idx++)
            {
                list = curl_slist_append(list, headers[idx].c_str());
            }

            if (list != nullptr)
            {
                CHECKIF(curl_easy_setopt(curl->Handle(), CURLOPT_HTTPHEADER, list) != CURLE_OK, E_FAIL);
            }

            try
            {
                CURLcode res = curl_easy_perform(curl->Handle());
                if(_silent == false)
                {
                    TraceVerbose("[%d] curl_easy_perform returned {%d}", callCount, static_cast<int>(res));
                }

                if (res != CURLcode::CURLE_OK)
                {
                    return E_FAIL;
                }
            }
            catch (...)
            {
                if(_silent == false)
                {
                    TraceError("curl_easy_perform failed");
                }

                curl_slist_free_all(list);
                return E_FAIL;
            }

            curl_easy_getinfo(curl->Handle(), CURLINFO_RESPONSE_CODE, &(response->statusCode));
            curl_slist_free_all(list);
            return hr;
        }
    };

    DLLAPI HRESULT CreateRESTClient(IRestAPI** ppObj, const char* ip, int port, bool silent)
    {
        return CurlImpl::Create(ppObj, ip, port, silent);
    }
}