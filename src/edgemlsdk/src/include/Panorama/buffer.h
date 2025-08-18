#ifndef __BUFFER_H__
#define __BUFFER_H__
#include <Panorama/comobj.h>

namespace Panorama
{
    DEF_INTERFACE(IBuffer, "{8F904259-6CDE-4C75-8B55-E2828E55345F}", IUnknownAlias)
    {
        /// @brief Gets the data
        /// @return The data
        virtual uint8_t* Data() const = 0;
        virtual int32_t Size() const = 0;
        virtual const char* AsString() const = 0;

        // todo: Deprecrate these methods
        inline int32_t AsInt() const
        {
            return *reinterpret_cast<const int32_t*>(&(Data()[0]));
        }

        inline float AsFloat() const
        {
            return *reinterpret_cast<const float*>(&(Data()[0]));
        }

        inline bool AsBoolean() const
        {
            return *reinterpret_cast<const bool*>(&(Data()[0]));
        }
    };

    DLLAPI HRESULT CreateBuffer(IBuffer** ppObj, int32_t size);
    DLLAPI HRESULT CreateBufferFromString(IBuffer** ppObj, const char* str);
    DLLAPI HRESULT CreateBufferFromFile(IBuffer** ppObj, const char* path);

    class Buffer
    {
    public:
        static HRESULT Create(IBuffer** ppObj, int32_t size)
        {
            return CreateBuffer(ppObj, size);
        }

        static HRESULT CreateFromString(IBuffer** ppObj, const char* str)
        {
            return CreateBufferFromString(ppObj, str);
        }

        static HRESULT CreateFromFile(IBuffer** ppObj, const char* path)
        {
            return CreateBufferFromFile(ppObj, path);
        }
    };
}

#endif