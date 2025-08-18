#ifndef __TRITON_H__
#define __TRITON_H__

#include <Panorama/comptr.h>
#include <Panorama/comobj.h>
#include <Panorama/buffer.h>
#include <Panorama/eventing.h>
#include <Panorama/mlops.h>
#include "triton/core/tritonserver.h"

#define CHECK_TRITON_RES(X) \
        {                                                                                                                                                       \
            TRITONSERVER_Error* err = X;                                                                                                                        \
            if (err != nullptr)                                                                                                                                 \
            {                                                                                                                                                   \
                TraceError("Error in triton server: (%s) %s", triton_err_strings[TRITONSERVER_ErrorCode(err)].c_str(), TRITONSERVER_ErrorMessage(err));         \
                TRITONSERVER_ErrorDelete(err);                                                                                                                  \
                hr = E_FAIL;                                                                                                                                    \
                goto Cleanup;                                                                                                                                   \
            }                                                                                                                                                   \
        }

inline static std::vector<std::string> triton_err_strings = { 
        "TRITONSERVER_ERROR_UNKNOWN"
        "TRITONSERVER_ERROR_INTERNAL",
        "TRITONSERVER_ERROR_NOT_FOUND",
        "TRITONSERVER_ERROR_INVALID_ARG",
        "TRITONSERVER_ERROR_UNAVAILABLE",
        "TRITONSERVER_ERROR_UNSUPPORTED",
        "TRITONSERVER_ERROR_ALREADY_EXISTS"};

namespace Panorama
{
    DEF_INTERFACE(ITritonRequest, "{A07DB75B-A4BA-445F-AC90-DE7219D969A1}", IInferenceRequest)
    {
        virtual TRITONSERVER_InferenceRequest* Get() = 0;
    };

    DEF_INTERFACE(ITritonServer, "{70CE687C-477B-4760-82A5-7EDDD701E756}", IInferenceServer)
    {
        virtual TRITONSERVER_Server* Get() = 0;
    };
}

#endif