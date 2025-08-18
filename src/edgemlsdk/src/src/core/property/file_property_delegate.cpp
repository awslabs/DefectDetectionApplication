#include <sstream>
#include <fstream>

#include <nlohmann/json.hpp>

#include <Panorama/properties.h>

#include <core/property/property_manager.h>
#include <scheduling.h>

using namespace Panorama;

class FilePropertyDelegate : public UnknownImpl<IPropertyDelegate>
{
public:
    static HRESULT Create(IPropertyDelegate** ppObj, const char* file)
    {
        HRESULT hr = S_OK;
        CREATE_COM(FilePropertyDelegate, ptr);
        CHECKHR(ptr->Initialize(file));

        *ppObj = ptr.Detach();
        return hr;
    }

    ~FilePropertyDelegate()
    {
        COM_DTOR(FilePropertyDelegate);
        COM_DTOR_FIN(FilePropertyDelegate);
    }

    HRESULT GetProperty(IProperty** ppObj, const char* property) override
    {
        return _propertyManager->GetProperty(ppObj, property);
    }

    HRESULT Synchronize(IPropertyCollection** ppObj) override
    {
        return ReadFile(ppObj);
    }

private:
    HRESULT Initialize(const char* file)
    {
        HRESULT hr = S_OK;
        CHECKNULL(file, E_INVALIDARG);
        CHECKHR(PropertyManager::Create(_propertyManager.AddressOf(), "file"));
        _file = file;
        CHECKHR(ReadFile(nullptr));

        return hr;
    }

    HRESULT ReadFile(IPropertyCollection** ppObj)
    {
        HRESULT hr = S_OK;

        std::ifstream fstream(_file);
        CHECKIF(fstream.is_open() == false, E_FAIL);
        std::stringstream ss;
        ss << fstream.rdbuf();

        CHECKIF_MSG(nlohmann::json::accept(ss.str().c_str()) == false, E_INVALID_STATE, "File does not contain valid json");
        nlohmann::json jObj = nlohmann::json::parse(ss.str().c_str());

        CHECKHR(_propertyManager->SetBatchProperty(ppObj, jObj));
        return hr;
    }


    ComPtr<PropertyManager> _propertyManager;
    std::string _file;
};

DLLAPI HRESULT CreateFilePropertyDelegate(IPropertyDelegate** ppObj, const char* file)
{
    return FilePropertyDelegate::Create(ppObj, file);
}