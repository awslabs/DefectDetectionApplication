#ifndef __CREDENTIALS_H__
#define __CREDENTIALS_H__

#include <aws/core/auth/AWSCredentialsProvider.h>
#include <Panorama/apidefs.h>
#include <Panorama/comobj.h>
#include <Panorama/buffer.h>

namespace Panorama
{
    DEF_INTERFACE(ICredentialProvider, "{8B2F72D1-442F-4A87-8E0D-D7F17396BE4F}", IUnknownAlias COMMA public Aws::Auth::AWSCredentialsProvider)
    {
        virtual std::shared_ptr<Aws::Auth::AWSCredentialsProvider> CredentialProvider() = 0;
        virtual HRESULT GetCredentialsAsJSON(IBuffer** ppObj) = 0;
    };
}

#endif