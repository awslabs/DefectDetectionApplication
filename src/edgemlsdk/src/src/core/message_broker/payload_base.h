#ifndef __PAYLOAD_BASE_H__
#define __PAYLOAD_BASE_H__

#include <Panorama/message_broker.h>

#include <string>

namespace Panorama
{
    class PayloadBase : virtual IPayload
    {
        public:
            const char* Id() override
            {
                return _id.c_str();
            }

            int64_t Timestamp() override
            {
                return _timestamp;
            }

            HRESULT SetTimestamp(int64_t timestamp) override
            {
                _timestamp = timestamp;
                return S_OK;
            }

            const char* CorrelationId() override
            {
                return _correlation_id.c_str();
            }

            HRESULT SetCorrelationId(const char* correlationId) override
            {
                CHECKNULL(correlationId, E_INVALIDARG);
                _correlation_id = correlationId;
                return S_OK;
            }

        protected:
            std::string _id;
            std::string _correlation_id;
            int64_t _timestamp;
    };
}

#endif
