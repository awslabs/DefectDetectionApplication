// Interface file for swig
%module(directors="1", threads="1") panorama_projections

%include "typemaps.i"
%include "stdint.i"
%include "cpointer.i"

typedef int int32_t;
typedef long long int64_t;
typedef int64_t Timestamp;
typedef int HRESULT;

%{
#include <Panorama/apidefs.h>
#include <Panorama/unknown.h>
#include <Panorama/chrono.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/trace.h>
#include <Panorama/buffer.h>
#include <Panorama/credentials.h>
#include <Panorama/properties.h>
#include <Panorama/app.h>

// videocapture.h must go before message_broker.h
// #include <Panorama/videocapture.h>
#include <Panorama/message_broker.h>
#include <Panorama/aws.h>
#include <Panorama/gst.h>
#include <Panorama/gst_application.h>
#include <Panorama/vector.h>
#include <Panorama/mlops.h>

// always include Panorama/python.h last
#include <Panorama/python.h>
%}

// This code is placed in panorama_wrap.cxx in PyObject *_wrap_Attach
%typemap(out) void Attach
{
    // Need the PyObject* passed down to C++ for internal 
    // reference counting, see python/embed.cpp
    Panorama::AttachProxy(arg1, arg2, swig_obj[1]);
    $result = SWIG_Py_Void();
}

%typemap(out) void PyObjectAddRef
{
    // Need the PyObject* passed down to C++ for internal 
    // reference counting, see python/embed.cpp
    Panorama::PyObjectAddRefProxy(arg1, swig_obj[0]);
    $result = SWIG_Py_Void();
}

%typemap(out) GstElement*
{
    $result = SWIG_From_long_SS_long((size_t)(result));
}

%typemap(out) uint8_t* {
    $result = SWIG_From_long_SS_long((size_t)(result));
}

// ======== Handler for any method that has a uint8_t** ppObj as input argument ======
%typemap(in, numinputs=0) uint8_t** ppObj (uint8_t* tmp)
%{
    $1 = &tmp;
%}

%typemap(argout) uint8_t** ppObj (PyObject* obj)
%{
    {
        size_t addr = reinterpret_cast<size_t>(*$1);
        PyObject* addrObj = PyLong_FromSize_t(addr);
        $result = SWIG_Python_AppendOutput($result,addrObj);
    }
%}

// ========= Mapping from a list to a char ** =================
%typemap(in) char ** {
  /* Check if is a list  */
  if (PyList_Check($input)) {
    int size = PyList_Size($input);
    int i = 0;
    $1 = (char **) malloc((size+1) * sizeof(char*));
    for (i = 0; i < size; i++) {
      PyObject *o = PyList_GetItem($input,i);
      if (PyString_Check(o))
        $1[i] = PyString_AsString(o);
      else {
        PyErr_SetString(PyExc_TypeError,"list must contain strings");
        free($1);
        return NULL;
      }
    }
    $1[i] = 0;
  } else if ($input == Py_None) {
    $1 =  NULL;
  } else {
    PyErr_SetString(PyExc_TypeError,"not a list");
    return NULL;
  }
}

// ============ Handler for any method that has IBuffer as output ======================================
%typemap(in, numinputs=0) Panorama::IBuffer** ppObj (Panorama::IBuffer* tmp)
%{
    $1 = &tmp;
%}

%typemap(argout) Panorama::IBuffer** ppObj (PyObject* obj)
%{
    {
        PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IBuffer, 0 |  0 );
        $result = SWIG_Python_AppendOutput($result,pyProp);
    }
%}

// ============ Handler for any method that has ITraceListener as output ======================================
%typemap(in, numinputs=0) Panorama::ITraceListener** ppObj (Panorama::ITraceListener* tmp)
%{
    $1 = &tmp;
%}

%typemap(argout) Panorama::ITraceListener** ppObj (PyObject* obj)
%{
    {
        PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__ITraceListener, 0 |  0 );
        $result = SWIG_Python_AppendOutput($result,pyProp);
    }
%}

// ============ Handler for any method that has IPipelineError as output ======================================
%typemap(in, numinputs=0) Panorama::IPipelineError** ppObj (Panorama::IPipelineError* tmp)
%{
    $1 = &tmp;
%}

%typemap(argout) Panorama::IPipelineError** ppObj (PyObject* obj)
%{
    {
        PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IPipelineError, 0 |  0 );
        $result = SWIG_Python_AppendOutput($result,pyProp);
    }
%}

// ============ Handler for any method that has IPipelineErrorCollection as output ======================================
%typemap(in, numinputs=0) Panorama::IPipelineErrorCollection** ppObj (Panorama::IPipelineErrorCollection* tmp)
%{
    $1 = &tmp;
%}

%typemap(argout) Panorama::IPipelineErrorCollection** ppObj (PyObject* obj)
%{
    {
        PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IPipelineErrorCollection, 0 |  0 );
        $result = SWIG_Python_AppendOutput($result,pyProp);
    }
%}

// ============ Handler for any method that has IPipeline as output ======================================
%typemap(in, numinputs=0) Panorama::IPipeline** ppObj (Panorama::IPipeline* tmp)
%{
    $1 = &tmp;
%}

%typemap(argout) Panorama::IPipeline** ppObj (PyObject* obj)
%{
    {
        PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IPipeline, 0 |  0 );
        $result = SWIG_Python_AppendOutput($result,pyProp);
    }
%}

// ============ Handler for any method that has IPipelineCollection as output ======================================
%typemap(in, numinputs=0) Panorama::IPipelineCollection** ppObj (Panorama::IPipelineCollection* tmp)
%{
    $1 = &tmp;
%}

%typemap(argout) Panorama::IPipelineCollection** ppObj (PyObject* obj)
%{
    {
        PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IPipelineCollection, 0 |  0 );
        $result = SWIG_Python_AppendOutput($result,pyProp);
    }
%}

// ============ Handler for any method that has IPipelineEventHandler as output ======================================
%typemap(in, numinputs=0) Panorama::IPipelineEventHandler** ppObj (Panorama::IPipelineEventHandler* tmp)
%{
    $1 = &tmp;
%}

%typemap(argout) Panorama::IPipelineEventHandler** ppObj (PyObject* obj)
%{
    {
        PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IPipelineEventHandler, 0 |  0 );
        $result = SWIG_Python_AppendOutput($result,pyProp);
    }
%}

// ============ Handler for any method that has IPipelineManager as output ======================================
%typemap(in, numinputs=0) Panorama::IPipelineManager** ppObj (Panorama::IPipelineManager* tmp)
%{
    $1 = &tmp;
%}

%typemap(argout) Panorama::IPipelineManager** ppObj (PyObject* obj)
%{
    {
        PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IPipelineManager, 0 |  0 );
        $result = SWIG_Python_AppendOutput($result,pyProp);
    }
%}

// ============ Handler for any method that has IApp as output ======================================
%typemap(in, numinputs=0) Panorama::IApp** ppObj (Panorama::IApp* tmp)
%{
    $1 = &tmp;
%}

%typemap(argout) Panorama::IApp** ppObj (PyObject* obj)
%{
    {
        PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IApp, 0 |  0 );
        $result = SWIG_Python_AppendOutput($result,pyProp);
    }
%}

// ============ Handler for any method that has I____Property as output ======================================
%typemap(in, numinputs=0) Panorama::IStringProperty** ppObj (Panorama::IStringProperty* tmp)
%{
    $1 = &tmp;
%}

%typemap(in, numinputs=0) Panorama::IIntegerProperty** ppObj (Panorama::IIntegerProperty* tmp)
%{
    $1 = &tmp;
%}

%typemap(in, numinputs=0) Panorama::IFloatProperty** ppObj (Panorama::IFloatProperty* tmp)
%{
    $1 = &tmp;
%}

%typemap(in, numinputs=0) Panorama::IBooleanProperty** ppObj (Panorama::IBooleanProperty* tmp)
%{
    $1 = &tmp;
%}

%typemap(in, numinputs=0) Panorama::IProperty** ppObj (Panorama::IProperty* tmp)
%{
    $1 = &tmp;
%}

%typemap(argout) Panorama::IStringProperty** ppObj (PyObject* obj)
%{
    {
        PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IStringProperty, 0 |  0 );
        $result = SWIG_Python_AppendOutput($result,pyProp);
    }
%}

%typemap(argout) Panorama::IIntegerProperty** ppObj (PyObject* obj)
%{
    {
        PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IIntegerProperty, 0 |  0 );
        $result = SWIG_Python_AppendOutput($result,pyProp);
    }
%}

%typemap(argout) Panorama::IFloatProperty** ppObj (PyObject* obj)
%{
    {
        PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IFloatProperty, 0 |  0 );
        $result = SWIG_Python_AppendOutput($result,pyProp);
    }
%}

%typemap(argout) Panorama::IBooleanProperty** ppObj (PyObject* obj)
%{
    {
        PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IBooleanProperty, 0 |  0 );
        $result = SWIG_Python_AppendOutput($result,pyProp);
    }
%}

%typemap(argout) Panorama::IProperty** ppObj (PyObject* obj)
%{
    {
        PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IProperty, 0 |  0 );
        $result = SWIG_Python_AppendOutput($result,pyProp);
    }
%}

// ============ Handler for any method that has IPropertyDelegate as output ======================================
%typemap(in, numinputs=0) Panorama::IPropertyDelegate** ppObj (Panorama::IPropertyDelegate* tmp)
%{
    $1 = &tmp;
%}

%typemap(argout) Panorama::IPropertyDelegate** ppObj (PyObject* obj)
%{
    {
        PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IPropertyDelegate, 0 |  0 );
        $result = SWIG_Python_AppendOutput($result,pyProp);
    }
%}

// ============ Handler for any method that has ICredentialProvider as output =============================================
%typemap(in, numinputs=0) Panorama::ICredentialProvider** ppObj (Panorama::ICredentialProvider* tmp)
%{
    $1 = &tmp;
%}

%typemap(argout) Panorama::ICredentialProvider** ppObj (PyObject* obj)
%{
    {
        PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__ICredentialProvider, 0 |  0 );
        $result = SWIG_Python_AppendOutput($result,pyProp);
    }
%}

// ============ Handler for any method that has IPropertyCollection as output =============================================
%typemap(in, numinputs=0) Panorama::IPropertyCollection** ppObj (Panorama::IPropertyCollection* tmp)
%{
    $1 = &tmp;
%}

%typemap(argout) Panorama::IPropertyCollection** ppObj (PyObject* obj)
%{
    {
        PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IPropertyCollection, 0 |  0 );
        $result = SWIG_Python_AppendOutput($result,pyProp);
    }
%}

// ============ Handler for any method that has IPropertyDelegate as output =============================================
%typemap(in, numinputs=0) Panorama::IPropertyDelegate** ppObj (Panorama::IPropertyDelegate* tmp)
%{
    $1 = &tmp;
%}

%typemap(argout) Panorama::IPropertyDelegate** ppObj (PyObject* obj)
%{
    {
        PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IPropertyDelegate, 0 |  0 );
        $result = SWIG_Python_AppendOutput($result,pyProp);
    }
%}

// ============ Handler for IPayload ==============================
%typemap(in, numinputs=0) Panorama::IPayload** ppObj (Panorama::IPayload* tmp)
%{
    $1 = &tmp;
%}

%typemap(argout) Panorama::IPayload** ppObj (PyObject* obj)
%{
    {
        PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IPayload, 0 |  0 );
        $result = SWIG_Python_AppendOutput($result,pyProp);
    }
%}

// ============ Handler for IBatchPayload ==============================
%typemap(in, numinputs=0) Panorama::IBatchPayload** ppObj (Panorama::IBatchPayload* tmp)
%{
    $1 = &tmp;
%}

%typemap(argout) Panorama::IBatchPayload** ppObj (PyObject* obj)
%{
    {
        PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IBatchPayload, 0 |  0 );
        $result = SWIG_Python_AppendOutput($result,pyProp);
    }
%}

// ============ Handler for IVideoPayload ==============================
// %typemap(in, numinputs=0) Panorama::IVideoPayload** ppObj (Panorama::IVideoPayload* tmp)
// %{
//     $1 = &tmp;
// %}

// %typemap(argout) Panorama::IVideoPayload** ppObj (PyObject* obj)
// %{
//     {
//         PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IVideoPayload, 0 |  0 );
//         $result = SWIG_Python_AppendOutput($result,pyProp);
//     }
// %}

// // ============ Handler for IVideoClip ==============================
// %typemap(in, numinputs=0) Panorama::IVideoClip** ppObj (Panorama::IVideoClip* tmp)
// %{
//     $1 = &tmp;
// %}

// %typemap(argout) Panorama::IVideoClip** ppObj (PyObject* obj)
// %{
//     {
//         PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IVideoClip, 0 |  0 );
//         $result = SWIG_Python_AppendOutput($result,pyProp);
//     }
// %}

// ============ Handler for IVideoClipCollection ==============================
// %typemap(in, numinputs=0) Panorama::IVideoClipCollection** ppObj (Panorama::IVideoClipCollection* tmp)
// %{
//     $1 = &tmp;
// %}

// %typemap(argout) Panorama::IVideoClipCollection** ppObj (PyObject* obj)
// %{
//     {
//         PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IVideoClipCollection, 0 |  0 );
//         $result = SWIG_Python_AppendOutput($result,pyProp);
//     }
// %}

// // ============ Handler for IVideoCaptureEventHandler ==============================
// %typemap(in, numinputs=0) Panorama::IVideoCaptureEventHandler** ppObj (Panorama::IVideoCaptureEventHandler* tmp)
// %{
//     $1 = &tmp;
// %}

// %typemap(argout) Panorama::IVideoCaptureEventHandler** ppObj (PyObject* obj)
// %{
//     {
//         PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IVideoCaptureEventHandler, 0 |  0 );
//         $result = SWIG_Python_AppendOutput($result,pyProp);
//     }
// %}

// // ============ Handler for IVideoCapture ==============================
// %typemap(in, numinputs=0) Panorama::IVideoCapture** ppObj (Panorama::IVideoCapture* tmp)
// %{
//     $1 = &tmp;
// %}

// %typemap(argout) Panorama::IVideoCapture** ppObj (PyObject* obj)
// %{
//     {
//         PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IVideoCapture, 0 |  0 );
//         $result = SWIG_Python_AppendOutput($result,pyProp);
//     }
// %}

// ============ Handler for any method that has IProtocolClient as output =============================================
%typemap(in, numinputs=0) Panorama::IProtocolClient** ppObj (Panorama::IProtocolClient* tmp)
%{
    $1 = &tmp;
%}

%typemap(argout) Panorama::IProtocolClient** ppObj (PyObject* obj)
%{
    {
        PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IProtocolClient, 0 |  0 );
        $result = SWIG_Python_AppendOutput($result,pyProp);
    }
%}

// =============== Handler protocol messages =============================================
%typemap(in, numinputs=0) Panorama::IProtocolMessage** ppObj (Panorama::IProtocolMessage* tmp)
%{
    $1 = &tmp;
%}

%typemap(in, numinputs=0) Panorama::IMqttMessage** ppObj (Panorama::IMqttMessage* tmp)
%{
    $1 = &tmp;
%}

%typemap(in, numinputs=0) Panorama::IS3Message** ppObj (Panorama::IS3Message* tmp)
%{
    $1 = &tmp;
%}

%typemap(in, numinputs=0) Panorama::IFileProtocolMessage** ppObj (Panorama::IFileProtocolMessage* tmp)
%{
    $1 = &tmp;
%}

%typemap(argout) Panorama::IProtocolMessage** ppObj (PyObject* obj)
%{
    {
        PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IProtocolMessage, 0 |  0 );
        $result = SWIG_Python_AppendOutput($result,pyProp);
    }
%}

%typemap(argout) Panorama::IMqttMessage** ppObj (PyObject* obj)
%{
    {
        PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IMqttMessage, 0 |  0 );
        $result = SWIG_Python_AppendOutput($result,pyProp);
    }
%}

%typemap(argout) Panorama::IS3Message** ppObj (PyObject* obj)
%{
    {
        PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IS3Message, 0 |  0 );
        $result = SWIG_Python_AppendOutput($result,pyProp);
    }
%}

%typemap(argout) Panorama::IFileProtocolMessage** ppObj (PyObject* obj)
%{
    {
        PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IFileProtocolMessage, 0 |  0 );
        $result = SWIG_Python_AppendOutput($result,pyProp);
    }
%}

// =============== Handler protocol subscriptions =============================================
%typemap(in, numinputs=0) Panorama::IProtocolSubscription** ppObj (Panorama::IProtocolSubscription* tmp)
%{
    $1 = &tmp;
%}

%typemap(in, numinputs=0) Panorama::IMqttSubscription** ppObj (Panorama::IMqttSubscription* tmp)
%{
    $1 = &tmp;
%}

%typemap(argout) Panorama::IProtocolSubscription** ppObj (PyObject* obj)
%{
    {
        PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IProtocolSubscription, 0 |  0 );
        $result = SWIG_Python_AppendOutput($result,pyProp);
    }
%}

%typemap(argout) Panorama::IMqttSubscription** ppObj (PyObject* obj)
%{
    {
        PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IMqttSubscription, 0 |  0 );
        $result = SWIG_Python_AppendOutput($result,pyProp);
    }
%}

// ============ Handler for any method that has IMessageBroker as output =============================================
%typemap(in, numinputs=0) Panorama::IMessageBroker** ppObj (Panorama::IMessageBroker* tmp)
%{
    $1 = &tmp;
%}

%typemap(argout) Panorama::IMessageBroker** ppObj (PyObject* obj)
%{
    {
        PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IMessageBroker, 0 |  0 );
        $result = SWIG_Python_AppendOutput($result,pyProp);
    }
%}


// ============ Handler for any method that has IInferenceServer as output =============================================
%typemap(in, numinputs=0) Panorama::IInferenceServer** ppObj (Panorama::IInferenceServer* tmp)
%{
    $1 = &tmp;
%}

%typemap(argout) Panorama::IInferenceServer** ppObj (PyObject* obj)
%{
    {
        PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IInferenceServer, 0 |  0 );
        $result = SWIG_Python_AppendOutput($result,pyProp);
    }
%}

// ============ Handler for any method that has IInferenceRequest as output =============================================
%typemap(in, numinputs=0) Panorama::IInferenceRequest** ppObj (Panorama::IInferenceRequest* tmp)
%{
    $1 = &tmp;
%}

%typemap(argout) Panorama::IInferenceRequest** ppObj (PyObject* obj)
%{
    {
        PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IInferenceRequest, 0 |  0 );
        $result = SWIG_Python_AppendOutput($result,pyProp);
    }
%}

// ============ Handler for any method that has ITensor as output =============================================
%typemap(in, numinputs=0) Panorama::ITensor** ppObj (Panorama::ITensor* tmp)
%{
    $1 = &tmp;
%}

%typemap(argout) Panorama::ITensor** ppObj (PyObject* obj)
%{
    {
        PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__ITensor, 0 |  0 );
        $result = SWIG_Python_AppendOutput($result,pyProp);
    }
%}

// ============ Handler for any method that has I____Vector as output =============================================
%typemap(in, numinputs=0) Panorama::IVector** ppObj (Panorama::IVector* tmp)
%{
    $1 = &tmp;
%}

%typemap(in, numinputs=0) Panorama::IInt64Vector** ppObj (Panorama::IInt64Vector* tmp)
%{
    $1 = &tmp;
%}

%typemap(argout) Panorama::IVector** ppObj (PyObject* obj)
%{
    {
        PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IVector, 0 |  0 );
        $result = SWIG_Python_AppendOutput($result,pyProp);
    }
%}

%typemap(argout) Panorama::IInt64Vector** ppObj (PyObject* obj)
%{
    {
        PyObject* pyProp = SWIG_NewPointerObj(SWIG_as_voidptr(*$1), SWIGTYPE_p_Panorama__IInt64Vector, 0 |  0 );
        $result = SWIG_Python_AppendOutput($result,pyProp);
    }
%}


// Enable cross-language polymorphism in the SWIG wrapper. 
%feature("director") IUnknownAlias;
%feature("director") IAppPlugin;
%feature("director") IMessageBrokerEventHandler;
%feature("director") IProtocolClientEventHandler;
%feature("director") IPropertyDelegateEventHandler;
%feature("director") IPipelineEventHandler;
%feature("director") IPipelineManagerEventHandler;
%feature("director") IPropertyEventHandler;
%feature("director") ITraceListener;
%feature("director") IProtocolClient;
%feature("director") IProtocolSubscription;
%feature("director") IProtocolMessage;
%feature("director") IProtocolFactory;
%feature("director") IPayload;

// Ignore inline functions that use ComPtr
%ignore GetClip(int32_t idx);
%ignore GenerateMultipleClipsAsync(int32_t clip_size_ms, int64_t ref_ts, float pos, VideoClipsGenerateCallback cb);
%ignore GenerateMultipleClipsAsync(int64_t ref_ts, float pos, VideoClipsGenerateCallback cb);
%ignore OnPropertyChanged(std::function<void(IProperty*)>);
%ignore Subscribe(const char* topic, MessageBrokerCallback cb);
%ignore OnRemoteCommand(const char* topic, RemoteCommandCalback cb);
%ignore OnError(std::function<void(IPipeline*, IPipelineError*)> cb);
%ignore OnStateChange(std::function<void(IPipeline* sender, int32_t oldState, int32_t newState)> cb);
%ignore OnPipelineAdded(std::function<void(IPipeline*)> cb);
%ignore OnRemoteCommand(const char* command, RemoteCommandCalback cb);
%ignore AuditPublish(PublishCallback cb);
%ignore GetPayload();
%ignore EventBroker;
%ignore Property;
%ignore MDS;
%ignore App;
%ignore Panorama_Aws;
%ignore GStreamer;
%ignore MessageBroker;
%ignore MLOps;
%ignore CreateInt64Vector(IInt64Vector** ppObj, const std::vector<int64_t>& input);

%include <Panorama/apidefs.h>
%include <Panorama/unknown.h>
%include <Panorama/chrono.h>
%include <Panorama/trace.h>
%include <Panorama/buffer.h>
%include <Panorama/credentials.h>
%include <Panorama/properties.h>
%include <Panorama/app.h>

// videocapture.h must go before message_broker.h
// %include <Panorama/videocapture.h>
%include <Panorama/message_broker.h>

%include <Panorama/aws.h>
%include <Panorama/gst.h>
%include <Panorama/gst_application.h>
%include <Panorama/vector.h>
%include <Panorama/mlops.h>

// always %include <Panorama.python.h> last
%include <Panorama/python.h>

%inline %{
%}
