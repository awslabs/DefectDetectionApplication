#ifndef __APP_H__
#define __APP_H__

#include <Panorama/comobj.h>
#include <Panorama/properties.h>
#include <Panorama/credentials.h>
#include <aws/core/auth/AWSCredentialsProvider.h>

namespace Panorama
{
    DEF_INTERFACE(IApp, "{E30A2E5F-4C36-4DA6-ABF4-A8CC7C409F54}", ICredentialProvider COMMA public IPropertyDelegate)
    {
        virtual HRESULT GetStringProperty(IStringProperty** ppObj, const char* property) = 0;
        virtual HRESULT GetIntegerProperty(IIntegerProperty** ppObj, const char* property) = 0;
        virtual HRESULT GetFloatProperty(IFloatProperty** ppObj, const char* property) = 0;
        virtual HRESULT GetBooleanProperty(IBooleanProperty** ppObj, const char* property) = 0;

        virtual HRESULT AddPropertyDelegate(IPropertyDelegate* propDelegate) = 0;
        virtual HRESULT RemovePropertyDelegate(IPropertyDelegate* propDelegate) = 0;
    };

    DEF_INTERFACE(IMDSClient, "{DF58C9F9-4256-4E1D-B070-5B9CE86BC975}", IUnknownAlias)
    {
        virtual HRESULT AnnounceSelf() = 0;
        virtual HRESULT Heartbeat(const char* errorCode, const char* status) = 0;
        virtual HRESULT GetPorts(IBuffer** ppObj) = 0;
        virtual HRESULT GetCredentials(IBuffer** ppObj) = 0;
    };

    DLLAPI HRESULT CreateMDSClient(IMDSClient** ppObj, const char* ip, int32_t port, const char* node_uid);
    DLLAPI HRESULT CreateMDSCredentialProvider(ICredentialProvider** ppObj, IMDSClient* mds_client);
    DLLAPI HRESULT CreateMDSPropertyDelegate(IPropertyDelegate** ppObj, IMDSClient* mds_client);
    DLLAPI HRESULT CreatePanoramaApp(IApp** ppObj, int argc, char** argv);

    class App
    {
    public:
        static ComPtr<IApp> CreateWithArgs(int argc, char** argv)
        {
            HRESULT hr = S_OK;
            ComPtr<IApp> ptr;
            CHECK_FAIL(Panorama::CreatePanoramaApp(ptr.AddressOf(), argc, argv), nullptr);
            return ptr;
        }

        static ComPtr<IApp> Create()
        {
            HRESULT hr = S_OK;
            ComPtr<IApp> ptr;
            CHECK_FAIL(Panorama::CreatePanoramaApp(ptr.AddressOf(), 0, nullptr), nullptr);
            return ptr;
        }
    };

    class MDS
    {
    public:
        static ComPtr<IMDSClient> Client(const char* ip, int port, const char* node_uid)
        {
            HRESULT hr = S_OK;
            ComPtr<IMDSClient> ptr;
            CHECK_FAIL(Panorama::CreateMDSClient(ptr.AddressOf(), ip, port, node_uid), nullptr);
            return ptr;
        }

        static ComPtr<ICredentialProvider> CredentialProvider(IMDSClient* mds_client)
        {
            HRESULT hr = S_OK;
            ComPtr<ICredentialProvider> ptr;
            CHECK_FAIL(Panorama::CreateMDSCredentialProvider(ptr.AddressOf(), mds_client), nullptr);
            return ptr;
        }

        static ComPtr<IPropertyDelegate> PropertyDelegate(IMDSClient* mds_client)
        {
            HRESULT hr = S_OK;
            ComPtr<IPropertyDelegate> ptr;
            CHECK_FAIL(Panorama::CreateMDSPropertyDelegate(ptr.AddressOf(), mds_client), nullptr);
            return ptr;
        }
    };
}

#endif