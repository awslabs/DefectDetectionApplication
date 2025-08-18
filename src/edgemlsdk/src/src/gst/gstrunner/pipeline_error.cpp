#include <nlohmann/json.hpp>

#include <Panorama/comptr.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/gst.h>
#include <collection_base.h>

using namespace Panorama;

class PipelineError: public UnknownImpl<IPipelineError>
{
public:
    static HRESULT Create(PipelineError** ppObj, GstMessage* msg)
    {
        return PipelineError::Create(reinterpret_cast<IPipelineError**>(ppObj), msg);
    }

    static HRESULT Create(IPipelineError** ppObj, GstMessage* msg)
    {
        HRESULT hr = S_OK;
        CREATE_COM(PipelineError, ptr);
        CHECKHR(ptr->Initialize(msg));
        *ppObj = ptr.Detach();
        return hr;
    }

    static HRESULT Create(IPipelineError** ppObj, int32_t msgType, int32_t domain, int32_t code)
    {
        HRESULT hr = S_OK;
        CREATE_COM(PipelineError, ptr);

        CHECKIF(msgType < -1, E_INVALIDARG);
        CHECKIF(domain > 5 || domain < -1, E_INVALIDARG);
        CHECKIF(code < -1, E_INVALIDARG);
        
        ptr->_element_name = "";
        ptr->_element_factory = "";

        CHECKHR(ptr->Initialize(msgType, domain, code));
        *ppObj = ptr.Detach();
        return hr;
    }

    ~PipelineError()
    {
        COM_DTOR_FIN(PipelineError);
    }

    const char* ErrorMessage() override
    {
        return _errorMsg.c_str();
    }

    const char* DebugInfo() override
    {
        return _debugInfo.c_str();
    }

    const char* DomainAsString() override
    {
        return _domainAsString.c_str();
    }

    uint32_t DomainQuark() override
    {
        return static_cast<uint32_t>(_quark);
    }

    int32_t MessageType() override
    {
        return _messageType;
    }

    ErrorDomain Domain() override
    {
        return _domain;
    } 

    int32_t Code() override
    {
        return _code;
    }

    const char* ElementName() override
    {
        return _element_name.c_str();
    }

    const char* ElementFactory() override
    {
        return _element_factory.c_str();
    }

    const char* ToString() override
    {
        return _json_representation.c_str();
    }

private:
    HRESULT Initialize(GstMessage* msg)
    {
        HRESULT hr = S_OK;

        // Get the name of the GstObject that generated the error
        _element_name = "";
        _element_factory = "";
        if(msg->src != nullptr)
        {
            // Get the name property if it exists
            GValue nameValue = G_VALUE_INIT;
            g_value_init(&nameValue, G_TYPE_STRING);
            g_object_get_property(G_OBJECT(msg->src), "name", &nameValue);
            _element_name = g_value_get_string(&nameValue);
            g_value_unset(&nameValue);

            // Get the factory name if it exists
            GstElementFactory* factory = gst_element_get_factory(GST_ELEMENT(msg->src));
            if (factory != nullptr) 
            {
                _element_factory = gst_plugin_feature_get_name(GST_PLUGIN_FEATURE(factory));
            }
        }

        GError* error;
        gchar *debug;
        _messageType = GST_MESSAGE_TYPE(msg);

        switch(_messageType)
        {
            case GST_MESSAGE_ERROR:
                gst_message_parse_error(msg, &error, &debug);
                break;
            case GST_MESSAGE_WARNING:
                gst_message_parse_warning(msg, &error, &debug);
                break;
            case GST_MESSAGE_EOS:
                _errorMsg = "End of Stream";
                _debugInfo = "";
                _code = 0;
                _quark = 0;
                _domainAsString = "";
                _domain = ErrorDomain::NOT_DEFINED;
                CreateJSONRepresentation();
                return S_OK;
            default:
                return E_NOTIMPL;
        }

        _errorMsg = error->message == nullptr ? "" : error->message;
        _debugInfo = debug == nullptr ? "" : debug;
        _code = error->code;
        _domainAsString = g_quark_to_string(error->domain);
        _quark = error->domain;
        // set domainAsString based on quark
        _domainAsString = g_quark_to_string(_quark);
        // set domain based on quark
        if(_quark == gst_core_error_quark())
        {
            _domain = ErrorDomain::CORE;
        }
        else if(_quark == gst_library_error_quark())
        {
            _domain = ErrorDomain::LIBRARY;
        }
        else if(_quark == gst_resource_error_quark())
        {
            _domain = ErrorDomain::RESOURCE;
        }
        else if(_quark == gst_stream_error_quark())
        {
            _domain = ErrorDomain::STREAM;
        }
        else
        {
            _domain = ErrorDomain::UNKNOWN;
        }
        
        g_error_free(error);
        g_free(debug);
        CreateJSONRepresentation();
        return hr;
    }

    HRESULT Initialize(int32_t msgType, int32_t domain, int32_t code)
    {
        HRESULT hr = S_OK;
        _messageType = msgType;
        _domain = ErrorDomain(domain);
        _code = code;
        _errorMsg = "";
        _debugInfo = "";
        if(_messageType == GST_MESSAGE_EOS)
        {
            _quark = 0;
            _domainAsString = "";
            CreateJSONRepresentation();
            return S_OK;
        }
        if(domain == ALL)
        {
            _quark = 0;
            _domainAsString = "ALL";
            CreateJSONRepresentation();
            return S_OK;
        }
        // domain to quark
        switch(_domain)
        {
            case ErrorDomain::CORE:
                _quark = gst_core_error_quark();
                break;
            case ErrorDomain::LIBRARY:
                _quark = gst_library_error_quark();
                break;
            case ErrorDomain::RESOURCE:
                _quark = gst_resource_error_quark();
                break;
            case ErrorDomain::STREAM:
                _quark = gst_stream_error_quark();
                break;
            default:
                return E_NOTIMPL;
        }
        _domainAsString = g_quark_to_string(_quark);
        CreateJSONRepresentation();
        return hr;
    }
    
    void CreateJSONRepresentation()
    {
        nlohmann::json err_jObj;

        err_jObj["factory"] = _element_factory;
        err_jObj["name"] = _element_name;
        err_jObj["debug_info"] = _debugInfo;
        err_jObj["code"] = _code;
        err_jObj["domain"] = static_cast<int32_t>(_domain);
        err_jObj["domain_string"] = _domainAsString;
        err_jObj["message"] = _errorMsg;

        switch(_messageType)
        {
            case GST_MESSAGE_ERROR:
                err_jObj["type_string"] = "error";
                err_jObj["type"] = static_cast<int32_t>(GST_MESSAGE_ERROR);
                break;
            case GST_MESSAGE_WARNING:
                err_jObj["type_string"] = "warning";
                err_jObj["type"] = static_cast<int32_t>(GST_MESSAGE_WARNING);
                break;
            case GST_MESSAGE_EOS:
                err_jObj["type_string"] = "eos";
                err_jObj["type"] = static_cast<int32_t>(GST_MESSAGE_EOS);
                break;
            default:
                err_jObj["type_string"] = "unknown";
                err_jObj["type"] = static_cast<int32_t>(GST_MESSAGE_UNKNOWN);
        }

        _json_representation = err_jObj.dump();
    }

    int32_t _code;
    int32_t _messageType;
    ErrorDomain _domain;
    std::string _errorMsg;
    std::string _debugInfo;
    GQuark _quark;
    std::string _domainAsString;
    std::string _element_name;
    std::string _element_factory;
    std::string _json_representation;
};

class PipelineErrorCollection : public CollectionBase<IPipelineErrorCollection, IPipelineError>
{
public:
    static HRESULT Create(IPipelineErrorCollection** ppObj)
    {
        HRESULT hr = S_OK;
        CREATE_COM(PipelineErrorCollection, ptr);
        *ppObj = ptr.Detach();
        return hr;
    }

    bool Contains(IPipelineError* pObj) override
    {
        HRESULT hr = S_OK;
        CHECKNULL(pObj, false);

        std::lock_guard<std::mutex> lk(_mtx);
        for (auto it = _elements.begin(); it != _elements.end(); ++it) 
        {
            ComPtr<IPipelineError> elem = static_cast<IPipelineError*>(*it);
            if((elem->MessageType()==GST_MESSAGE_EOS || elem->MessageType()==ALL) && 
                pObj->MessageType()==GST_MESSAGE_EOS)
                return true;

            auto msgType = elem->MessageType();
            auto mstType2 = pObj->MessageType();
            if( (elem->MessageType()==pObj->MessageType() || elem->MessageType()==ALL) && 
                (elem->Domain()==pObj->Domain() || int(elem->Domain())==ALL) && 
                (elem->Code()==pObj->Code() || elem->Code()==ALL) )
                return true;
        }
        return false;
    }

    ~PipelineErrorCollection()
    {
        COM_DTOR_FIN(PipelineErrorCollection);
    }

private:
    PipelineErrorCollection() = default;
};


DLLAPI HRESULT CreatePipelineError(IPipelineError** ppObj, int32_t msgType, int32_t domain, int32_t code)
{
    return PipelineError::Create(ppObj, msgType, domain, code);
}

DLLAPI HRESULT CreatePipelineErrorFromMessage(IPipelineError** ppObj, GstMessage* msg)
{
    return PipelineError::Create(ppObj, msg);
}

DLLAPI HRESULT CreatePipelineErrorCollection(IPipelineErrorCollection** ppObj)
{
    return PipelineErrorCollection::Create(ppObj);
}