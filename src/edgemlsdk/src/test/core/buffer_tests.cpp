
#include <thread>
#include <fstream>

#include <gtest/gtest.h>
#include <Panorama/comptr.h>
#include <Panorama/buffer.h>
#include <TestUtils.h>

using namespace std;
using namespace Panorama;

TEST(BufferTests, Create)
{
    HRESULT hr = S_OK;

    ComPtr<IBuffer> buffer;
    ASSERT_F(Buffer::Create(nullptr, 1));
    ASSERT_F(Buffer::Create(nullptr, -1));
    ASSERT_F(Buffer::Create(buffer.AddressOf(), -1));

    ASSERT_S(Buffer::Create(buffer.AddressOf(), 1024));
    ASSERT_EQ(buffer->Size(), 1024);
    ASSERT_TRUE(buffer->Data());

    ComPtr<IBuffer> buffer1;
    ASSERT_S(Buffer::Create(buffer1.AddressOf(), 0));
    ASSERT_EQ(buffer1->Size(), 0);
    // Buffer is empty and underlying vector has size 0. No guarantee vector has any data in it, don't check Data()
}

TEST(BufferTests, CreateFromString)
{
    HRESULT hr = S_OK;

    ComPtr<IBuffer> buffer;
    ASSERT_F(Buffer::CreateFromString(nullptr, nullptr));
    ASSERT_F(Buffer::CreateFromString(buffer.AddressOf(), nullptr));
    ASSERT_F(Buffer::CreateFromString(nullptr, "hello world"));

    ASSERT_S(Buffer::CreateFromString(buffer.AddressOf(), "hello world"));
    ASSERT_EQ(buffer->Size(), 12);
    ASSERT_TRUE(buffer->Data());
    ASSERT_EQ(strcmp("hello world", buffer->AsString()), 0);

    ComPtr<IBuffer> buffer2;
    ASSERT_S(Buffer::CreateFromString(buffer2.AddressOf(), ""));
    ASSERT_EQ(buffer2->Size(), 1);
    ASSERT_TRUE(buffer2->Data());
    ASSERT_EQ(strcmp("", buffer2->AsString()), 0);
}
