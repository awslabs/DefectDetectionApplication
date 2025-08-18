#include <Panorama/properties.h>

using namespace Panorama;

HRESULT PropertyEventHandlerImpl::Create(IPropertyEventHandler** ppObj, std::function<void(IProperty*)> cb)
{
    HRESULT hr = S_OK;
    CREATE_COM(PropertyEventHandlerImpl, ptr);
    ptr->_cb = std::move(cb);
    *ppObj = ptr.Detach();
    return hr;
}

PropertyEventHandlerImpl::~PropertyEventHandlerImpl()
{
    COM_DTOR_FIN(PropertyEventHandlerImpl);
}

void PropertyEventHandlerImpl::OnPropertyChanged(IProperty* property)
{
    if(_cb)
    {
        _cb(property);
    }
}