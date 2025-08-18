#ifndef __FLOWCONTROL_H__
#define __FLOWCONTROL_H__
#include <assert.h>
#include <Panorama/apidefs.h>
#include <Panorama/trace.h>

#define CHECKIF_MSG(X, RET, FMT, ...) if(X) { Panorama::Tracer::Trace(Panorama::TraceLevel::Error, Panorama::NowAsTimestamp(), __LINE__, __FILENAME__, "[CHECKIF " #X "]: " FMT, ##__VA_ARGS__); return RET; }
#define CHECKIF(X, RET) CHECKIF_MSG(X, RET, "")

#define CHECKNULL_MSG(X, RET, FMT, ...) if(X == nullptr) { Panorama::Tracer::Trace(Panorama::TraceLevel::Error, Panorama::NowAsTimestamp(), __LINE__, __FILENAME__, "[CHECKNULL " #X "]: " FMT, ##__VA_ARGS__); return RET; }
#define CHECKNULL(X, RET) CHECKNULL_MSG(X, RET, "")

#define CHECKHR_MSG(X, FMT, ...) hr = X; if(FAILED(hr)){ Panorama::Tracer::Trace(Panorama::TraceLevel::Error, Panorama::NowAsTimestamp(), __LINE__, __FILENAME__, "[CHECKHR " #X " (%s)]: " FMT, ErrorCodeToString(hr), ##__VA_ARGS__); return hr; }
#define CHECKHR(X) CHECKHR_MSG(X, "")

#define CHECK_FAIL_MSG(X, RET, FMT, ...) hr = X; if(FAILED(hr)){ Panorama::Tracer::Trace(Panorama::TraceLevel::Error, Panorama::NowAsTimestamp(), __LINE__, __FILENAME__, "[CHECKFAIL " #X " (%s)]: " FMT, ErrorCodeToString(hr), ##__VA_ARGS__); return RET; }
#define CHECK_FAIL(X, RET) CHECK_FAIL_MSG(X, RET, "")

#define CHECKNULL_OR_EMPTY(STR, HR) CHECKNULL(STR, HR); CHECKIF(strlen(STR) == 0, HR);


#define PEEKHR_MSG(X, FMT, ...) { HRESULT __peek_hr = X; if(FAILED(__peek_hr)) {Panorama::Tracer::Trace(Panorama::TraceLevel::Warning, Panorama::NowAsTimestamp(), __LINE__, __FILENAME__, FMT, ##__VA_ARGS__);} }
#define PEEKHR(X) PEEKHR_MSG(X, "[%s] failed: %s", #X, ErrorCodeToString(__peek_hr))

#define __assert(X, MSG) if(X == false){ TraceError(MSG); assert(false); }
#endif