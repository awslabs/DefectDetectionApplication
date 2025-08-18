#ifndef __PYTHON_H__
#define __PYTHON_H__
#include <Panorama/apidefs.h>
#include <Panorama/message_broker.h>
#include <Panorama/unknown.h>
#include <Panorama/properties.h>

namespace Panorama
{
    // Python sys
    DLLAPI void AppendPythonPath(const char* path);
    DLLAPI void AllowPythonThread();
    
    // Memory Management
    DLLAPI void Attach(IUnknownAlias** ppObj, IUnknownAlias* obj);
    DLLAPI void AttachProxy(IUnknownAlias** ppObj, IUnknownAlias* obj, void* pyObject);
    
    DLLAPI void PyObjectAddRef(IUnknownAlias* obj);
    DLLAPI void PyObjectAddRefProxy(IUnknownAlias* obj, void* pyObject);

    DLLAPI void AddrToCPointer(unsigned char** obj, size_t address);
    DLLAPI void PyObjectRelease(IUnknownAlias* obj);

    DLLAPI HRESULT PythonQueryInterface(IUnknownAlias* self, const char* targetUuid);
    DLLAPI HRESULT PythonQueryInterfaceBatchPayload(IBatchPayload** ppObj, IUnknownAlias* self);
    DLLAPI HRESULT PythonQueryInterfaceStringProperty(IStringProperty** ppObj, IUnknownAlias* self);
    DLLAPI HRESULT PythonQueryInterfaceIntegerProperty(IIntegerProperty** ppObj, IUnknownAlias* self);
    DLLAPI HRESULT PythonQueryInterfaceFloatProperty(IFloatProperty** ppObj, IUnknownAlias* self);
    DLLAPI HRESULT PythonQueryInterfaceBooleanProperty(IBooleanProperty** ppObj, IUnknownAlias* self);
}

#endif