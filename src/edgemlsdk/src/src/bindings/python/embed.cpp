#include <map>
#include <mutex>


#include <Panorama/python.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/gst_application.h>

#if defined(_DEBUG)
# undef _DEBUG
#include <Python.h>
#include <corecrt.h>
# define _DEBUG 1
#else
# include <Python.h>
#endif

#include "panorama_projections_python_runtime.h"

using namespace Panorama;

static std::map<IUnknownAlias*, PyObject*> pyObjs;
static std::mutex pyObjMtx;
static std::mutex pyThreadMtx;

DLLAPI void AppendPythonPath(const char* path)
{
    Py_Initialize();
    PyObject* sys_path = PySys_GetObject("path");
    PyList_Append(sys_path, PyUnicode_FromString(path));

    TraceVerbose("Appending python path: %s", path);
}

DLLAPI void AllowPythonThread()
{
    std::lock_guard<std::mutex> lk(pyThreadMtx);
    static bool pyThreadIsAllowed = false;
    if (!PyEval_ThreadsInitialized())
    {
        PyEval_InitThreads();
    }
    if (pyThreadIsAllowed == false)
    {
        PyThreadState* _state = PyEval_SaveThread();
        pyThreadIsAllowed = true;
    }
}

DLLAPI void AddrToCPointer(unsigned char** obj, size_t address)
{
    *obj = reinterpret_cast<unsigned char*>(address);
}

DLLAPI void PyObjectRelease(IUnknownAlias* obj)
{
    CHECKNULL_MSG(obj, , "Attempting to release null object");
    
    void* pObj;
    HRESULT isTraceListener = obj->QueryInterface(internal_uuidof<ITraceListener>(), &pObj);

    if(FAILED(isTraceListener))
    {
        TraceVerbose("Releasing Python Object at %p", obj);
    }

    if (pyObjs.find(obj) == pyObjs.end())
    {
        if(FAILED(isTraceListener))
        {
            TraceVerbose("No native reference to Python Object at %p", obj);
        }

        return;
    }

    if(FAILED(isTraceListener))
    {
        TraceVerbose("Native reference to Python Object released at %p", obj);
    }

    std::lock_guard<std::mutex> lk(pyObjMtx);
    Py_XDECREF(pyObjs[obj]);
    pyObjs.erase(obj);
}

DLLAPI void PyObjectAddRef(IUnknownAlias* obj)
{
    // Only here because of interaction with auto generated code from swig
}

DLLAPI void PyObjectAddRefProxy(IUnknownAlias* obj, void* pyObject)
{
    PyObject* ptr = reinterpret_cast<PyObject*>(pyObject);
    if (pyObjs.find(obj) != pyObjs.end())
    {
        // C code only maintains a single reference to the Python Object
        TraceVerbose("Native code already has reference to Python Object at %p", obj);
    }
    else
    {
        TraceVerbose("Obtaining native reference to Python Object at %p", obj);
        pyObjs[obj] = ptr;
        Py_XINCREF(ptr);
    }
}

DLLAPI void AttachProxy(IUnknownAlias** ppObj, IUnknownAlias* obj, void* pyObject)
{
    PyObject* ptr = reinterpret_cast<PyObject*>(pyObject);
    if (pyObjs.find(obj) != pyObjs.end())
    {
        // C code only maintains a single reference to the Python Object
        TraceVerbose("Native code already has reference to Python Object");
    }
    else
    {
        TraceVerbose("Obtaining native reference to Python Object");
        pyObjs[obj] = ptr;
        Py_XINCREF(ptr);
    }

    *ppObj = obj;
    obj->AddRef();
}

DLLAPI void Attach(IUnknownAlias** ppObj, IUnknownAlias* obj)
{
    // Only here because of interaction with auto generated code from swig
}

template<typename T>
T* Python2Interface(PyObject* obj, const char* className) {
    void* argp1 = 0;
    swig_type_info* pTypeInfo = SWIG_TypeQuery(className);

    const int res = SWIG_ConvertPtr(obj, &argp1, pTypeInfo, 0);
    CHECKIF_MSG(!SWIG_IsOK(res), nullptr, "Could not create python object");
    return reinterpret_cast<T*>(argp1);
}

template<typename T>
T* GetPyObject(const char* moduleName, const char* className, const char* interfaceName)
{
    if(Py_IsInitialized() == false)
    {
        Py_Initialize();
    }

    AllowPythonThread();

    PyGILState_STATE gstate;
    gstate = PyGILState_Ensure();

    TraceVerbose("Embedding %s from %s", className, moduleName);
    PyObject* module = PyImport_ImportModule(moduleName);

    if (module == nullptr)
    {
        PyErr_Print();
        CHECKIF_MSG(true, nullptr, "Could not import module");
    }

    PyObject* cls = PyObject_GetAttrString(module, className);
    CHECKNULL_MSG(cls, nullptr, "PyObject_GetAttrString failed");

    PyObject* instance = PyObject_CallFunctionObjArgs(cls, NULL);
    if(PyErr_Occurred())
    {
        PyErr_Print();
    }

    CHECKNULL_MSG(instance, nullptr, "PyObject_CallFunctionObjArgs failed");

    T* inst = Python2Interface<T>(instance, interfaceName);

    std::lock_guard<std::mutex> lk(pyObjMtx);
    pyObjs[inst] = instance;

    TraceVerbose("Python Object created at %p", inst);
    Py_XDECREF(cls);
    PyGILState_Release(gstate);
    return inst;
}

#define GET_PY_OBJECT(TYPE, MODULE, CLASS) GetPyObject<TYPE>(MODULE, CLASS, #TYPE)

DLLAPI HRESULT EmbedAppPlugin(IAppPlugin** ppObj, const char* moduleName, const char* className)
{
    HRESULT hr = S_OK;
    CHECKNULL(ppObj, E_POINTER);

    ComPtr<IAppPlugin> ptr;
    ptr = GET_PY_OBJECT(IAppPlugin, moduleName, className);
    CHECKNULL(ptr, E_FAIL);
    *ppObj = ptr.Detach();
    return hr;
}

DLLAPI HRESULT PythonQueryInterface(IUnknownAlias* self, const char* targetUuid)
{
    HRESULT hr = S_OK;
    CHECKNULL(self, E_INVALIDARG);
    CHECKNULL_OR_EMPTY(targetUuid, E_INVALIDARG);
    CHECKIF(strlen(targetUuid) < 36, E_INVALIDARG);

    Guid uuid = GuidFromString(targetUuid);
    void* obj;
    return self->QueryInterface(uuid, &obj);
}

#define QI_IMPL(NAME) \
    DLLAPI HRESULT PythonQueryInterface##NAME(I##NAME** ppObj, IUnknownAlias* self)                     \
    {                                                                                                   \
        HRESULT hr = S_OK;                                                                              \
        CHECKNULL(ppObj, E_POINTER);                                                                    \
        CHECKHR(PythonQueryInterface(self, GuidToString(internal_uuidof<I##NAME>()).c_str()));          \
        *ppObj = dynamic_cast<I##NAME*>(self);                                                          \
        return hr;                                                                                      \
    }                                                                                                   

QI_IMPL(BatchPayload);
QI_IMPL(StringProperty);
QI_IMPL(IntegerProperty);
QI_IMPL(FloatProperty);
QI_IMPL(BooleanProperty);