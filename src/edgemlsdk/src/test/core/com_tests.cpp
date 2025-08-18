#include <thread>
#include <gtest/gtest.h>

#include <Panorama/comobj.h>
#include <Panorama/comptr.h>
#include <Panorama/flowcontrol.h>

#include "../TestUtils.h"

using namespace std;
using namespace Panorama;

class Foo : public UnknownImpl<IUnknownAlias>
{
};

namespace Panorama
{
    DEF_INTERFACE(IFoobar, "{C390C17A-B898-4F3D-A6C7-A30AACC37C49}", IUnknownAlias
    {
        virtual int32_t X() = 0;
    });
}

class Foobar : public UnknownImpl<IFoobar>
{
public:
    int32_t X() override
    {
        return 5;
    }
};

class Foobar2 : public UnknownImpl<IUnknownAlias>
{
public:
    static HRESULT Create(IUnknownAlias** ppObj)
    {
        HRESULT hr = S_OK;
        CREATE_COM(Foobar2, ptr);
        *ppObj = ptr.Detach();
        return hr;
    }

    ~Foobar2()
    {
        COM_DTOR_FIN(Foobar2);
    }
};

TEST(ComTests, ComPtrRefCount)
{
    // Simple Add/Release calls
    {
        ComPtr<IUnknownAlias> foo;
        foo.Attach(new (std::nothrow) Foo());

        ASSERT_FALSE(foo == nullptr);
        foo.AddRef();
        foo.AddRef();

        foo.Release();
        foo.Release();
        ASSERT_FALSE(foo == nullptr);
        foo.Release();
        ASSERT_TRUE(foo == nullptr);
    }

    // Ref Count by exiting scope and copy ctor
    {
        ComPtr<IUnknownAlias> foo;
        {
            ComPtr<IUnknownAlias> foo2;
            foo2.Attach(new (std::nothrow) Foo());
            foo = foo2;
        }

        ASSERT_FALSE(foo == nullptr);
        foo.Release();
        ASSERT_TRUE(foo == nullptr);
    }
}

TEST(ComTests, QueryInterface)
{
    ASSERT_FALSE(uuidof(IFoobar) == uuidof(IUnknownAlias));
    
    ComPtr<Foobar> foobar;
    foobar.Attach(new (std::nothrow) Foobar());
    
    ComPtr<IUnknownAlias> unk = static_cast<IUnknownAlias*>(foobar._ptr);
    ComPtr<IFoobar> foobar2 = unk.QueryInterface<IFoobar>();
    ASSERT_TRUE(foobar2 != nullptr);
    ASSERT_TRUE(foobar2 == foobar);
    ASSERT_TRUE(foobar2->X() == 5);
}

TEST(ComTests, Assignment)
{
    ComPtr<IUnknownAlias> com1, com2;

    Foobar2::Create(com1.AddressOf());
    Foobar2::Create(com2.AddressOf());

    ASSERT_TRUE(com1->RefCount() == 1);
    ASSERT_TRUE(com2->RefCount() == 1);

    com1 = com2;
    ASSERT_TRUE(com2->RefCount() == 2);
}

TEST(ComTests, ValidateVersion)
{
    // Not really a COM test, but it's part of api defs, so w/e
    ASSERT_EQ(strcmp(GetVersionString(), __SDK_VERSION__), 0);
}