#ifndef __GUID_H__
#define __GUID_H__

#include <cstring>
#include <string>
#include <iomanip>
#include <sstream>
#include <random>
#include <algorithm>
#include <cctype>

struct Guid
{
    uint32_t Data1;
    uint16_t Data2;
    uint16_t Data3;
    uint8_t Data4[8];

    Guid()
    {
        memset(this, 0, sizeof(Guid));
    }

    bool operator==(const Guid& other) const
    {
        return memcmp(this, &other, sizeof(Guid)) == 0;
    }

    bool operator!=(const Guid& other) const
    {
        return !(this->operator==(other));
    }
};

inline Guid GuidFromString(const char* str)
{
    if (str[0] == '{')
    {
        return GuidFromString(str + 1);
    }

    char ch;
    Guid g;

    std::istringstream ss(str);
    ss >> std::hex >> g.Data1 >> ch;
    ss >> std::hex >> g.Data2 >> ch;
    ss >> std::hex >> g.Data3 >> ch;

    uint16_t data4_01;
    uint64_t data4_27;

    ss >> std::hex >> data4_01 >> ch;
    ss >> std::hex >> data4_27;

    g.Data4[0] = static_cast<uint8_t>(data4_01 >> 8);
    g.Data4[1] = static_cast<uint8_t>(data4_01 & 0x00FF);
    g.Data4[2] = static_cast<uint8_t>(data4_27 >> 40);
    g.Data4[3] = static_cast<uint8_t>((data4_27 & 0x000000FF00000000) >> 32);
    g.Data4[4] = static_cast<uint8_t>((data4_27 & 0x00000000FF000000) >> 24);
    g.Data4[5] = static_cast<uint8_t>((data4_27 & 0x0000000000FF0000) >> 16);
    g.Data4[6] = static_cast<uint8_t>((data4_27 & 0x000000000000FF00) >> 8);
    g.Data4[7] = static_cast<uint8_t>(data4_27 & 0x00000000000000FF);

    return g;
}

inline std::string GuidToString(const Guid& guid)
{
    std::stringstream stream;
    stream << std::hex << std::setfill('0')
           << std::setw(8) << guid.Data1 << '-'
           << std::setw(4) << guid.Data2 << '-'
           << std::setw(4) << guid.Data3 << '-';

    // Data4 is an array of bytes, so we need to handle it separately
    stream << std::setw(2) << static_cast<int>(guid.Data4[0]);
    stream << std::setw(2) << static_cast<int>(guid.Data4[1]) << '-';
    for (int i = 2; i < 8; ++i)
    {
        stream << std::setw(2) << static_cast<int>(guid.Data4[i]);
    }

    std::string upper = stream.str();
    std::transform(upper.begin(), upper.end(), upper.begin(), [](unsigned char c) { return std::toupper(c); });
    return upper;
}

inline Guid GenerateGuid()
{
    static std::random_device rd;
    static std::mt19937 generator(rd());
    static std::uniform_int_distribution<int64_t> distribution_64(0, INT64_MAX);
    static std::uniform_int_distribution<int32_t> distribution_32(0, INT32_MAX);
    static std::uniform_int_distribution<int16_t> distribution_16(0, INT16_MAX);

    Guid g;
    g.Data1 = distribution_32(generator);
    g.Data2 = distribution_16(generator);
    g.Data3 = distribution_16(generator);

    int64_t val = distribution_64(generator);
    memcpy(g.Data4, &val, sizeof(int64_t));

    return g;
}

#endif