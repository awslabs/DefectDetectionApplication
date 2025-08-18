#include <Panorama/apidefs.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/comptr.h>
#include <Panorama/properties.h>

#include "property_manager.h"

using namespace Panorama;
class CommandLinePropertyDelegate : public UnknownImpl<IPropertyDelegate>
{
public:
    static HRESULT Create(IPropertyDelegate** ppObj, int argc, char* argv[])
    {
        HRESULT hr = S_OK;
        CREATE_COM(CommandLinePropertyDelegate, ptr);
        CHECKHR(ptr->Initialize(argc, argv));
        *ppObj = ptr.Detach();
        return hr;
    }

    ~CommandLinePropertyDelegate()
    {
        COM_DTOR_FIN(CommandLinePropertyDelegate);
    }

    HRESULT GetProperty(IProperty** ppObj, const char* property) override
    {
        return _propertyManager->GetProperty(ppObj, property);
    }

    HRESULT Synchronize(IPropertyCollection** ppObj) override
    {
        return CreatePropertyCollection(ppObj);
    }

private:
    CommandLinePropertyDelegate() = default;

    bool isInteger(const std::string &s)
    {
        try 
        {
            std::stoi(s);
            return true;
        } 
        catch(...)
        {
            return false;
        }
    }

    bool isFloat(const std::string &s)
    {
        try
        {
            std::stof(s);
            return true;
        }
        catch(...)
        {
            return false;
        }
    }

    int characterCount(const std::string& s, char character)
    {
        int count = 0;
        for(auto iter = s.begin(); iter != s.end(); iter++)
        {
            if(*iter == character)
            {
                count++;
            }
        }

        return count;
    }

    HRESULT AddProperty(std::string& key, std::string &value)
    {
        HRESULT hr = S_OK;

        key = key.erase(0, 2);

        if(characterCount(value, '.') == 0 && isInteger(value))
        {
            CHECKHR(_propertyManager->SetProperty(key.c_str(), stoi(value)));
        }
        else if(characterCount(value, '.') == 1 && isFloat(value))
        {
            // float property
            CHECKHR(_propertyManager->SetProperty(key.c_str(), static_cast<float>(stod(value))));
        }
        else if(value.empty())
        {
            // boolean property
            CHECKHR(_propertyManager->SetProperty(key.c_str(), true));
        }
        else
        {
            CHECKHR(_propertyManager->SetProperty(key.c_str(), value.c_str()));
        }

        return hr;
    }

    HRESULT Initialize(int argc, char* argv[])
    {
        HRESULT hr = S_OK;
        CHECKHR(PropertyManager::Create(_propertyManager.AddressOf(), "CommandLine"));

        if(argc == 1)
        {
            return S_OK;
        }
        
        std::string value = "";
        std::string newProperty;
        for(int i = 1; i < argc; i++)
        {
            std::string opt = argv[i];

            int idx = opt.find("--");
            if(idx == 0)
            {
                // new property
                if(newProperty.empty() == false)
                {
                    CHECKHR(AddProperty(newProperty, value));
                    value = "";
                }

                CHECKIF(value.empty() == false, E_FAIL);
                newProperty = argv[i];
            }
            else
            {
                // value for property
                value = value + (value.empty() ? "" : " ") + argv[i];
            }
        }

        CHECKIF(newProperty.empty(), E_FAIL);
        CHECKHR(AddProperty(newProperty, value));
        return hr;
    }

    ComPtr<PropertyManager> _propertyManager;
};

DLLAPI HRESULT CreateCLIPropertyDelegate(IPropertyDelegate** ppObj, int argc, char** argv)
{
    return CommandLinePropertyDelegate::Create(ppObj, argc, argv);
}