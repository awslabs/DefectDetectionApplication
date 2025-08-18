#include <Panorama/properties.h>
#include <collection_base.h>

using namespace Panorama;

class PropertyCollection : public CollectionBase<IPropertyCollection, IProperty>
{
public:
    static HRESULT Create(IPropertyCollection** ppObj)
    {
        HRESULT hr = S_OK;
        CREATE_COM(PropertyCollection, ptr);
        *ppObj = ptr.Detach();
        return hr;
    }

    ~PropertyCollection()
    {
        COM_DTOR_FIN(PropertyCollection);
    }

    bool ContainsKey(const char* key) override
    {
        for(auto iter = _elements.begin(); iter != _elements.end(); iter++)
        {
            if(strcmp(key, iter->Ptr()->ID()) == 0)
            {
                return true;
            }
        }

        return false;
    }
};

DLLAPI HRESULT CreatePropertyCollection(IPropertyCollection** ppObj)
{
    return PropertyCollection::Create(ppObj);
}