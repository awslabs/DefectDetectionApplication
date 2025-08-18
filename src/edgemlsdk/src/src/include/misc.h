#ifndef __MISC_H__
#define __MISC_H__

#include <sstream>
#include <string>
#include <vector>
#include <regex>
#include <nlohmann/json.hpp>

inline std::vector<std::string> SplitString(const std::string& input, char delimeter)
{
    std::vector<std::string> strings;

    std::string s;
    std::stringstream ss;
    ss << input;

    while (getline(ss, s, delimeter)) 
    {
        strings.push_back(s);
    }

    return strings;
}

inline std::vector<std::string> EncapsulatedStrings(const std::string& input, const char* left, const char* right)
{
    std::stringstream pattern;
    pattern << left << "([^}]*)" << right;

    std::regex regex(pattern.str().c_str());
    std::sregex_iterator next(input.begin(), input.end(), regex);
    std::sregex_iterator end;

    std::vector<std::string> encapStrs;
    while (next != end) 
    {
        std::smatch match = *next;
        encapStrs.push_back(match.str(1));
        next++;
    }

    return encapStrs;
}

inline void FindAndReplace(std::string& input, const char* find, const char *replace)
{
    if(find == nullptr || replace == nullptr)
    {
        return;
    }

    int len = strlen(find);
    int32_t pos = 0;

    do
    {
        int32_t idx = input.find(find, pos);
        if(idx == input.npos)
        {
            break;
        }

        input.replace(idx, len, replace);
        pos = idx + strlen(replace);
    }while(true);
}

inline std::string MergeJson(const std::string& primary, const std::string& secondary)
{
    nlohmann::json primaryObj;
    nlohmann::json secondaryObj;

    if(nlohmann::json::accept(primary))
    {
        primaryObj = nlohmann::json::parse(primary);
    }
    else
    {
        return "";
    }

    if(nlohmann::json::accept(secondary))
    {
        secondaryObj = nlohmann::json::parse(secondary);
    }
    else
    {
        return "";
    }

    if(primaryObj.is_array() && secondaryObj.is_array())
    {
        return primaryObj.dump();
    }
    else if(primaryObj.is_array() || secondaryObj.is_array())
    {
        return "";
    }

    for (nlohmann::json::iterator it = primaryObj.begin(); it != primaryObj.end(); it++)
    {
        secondaryObj[it.key()] = it.value();
    }

    return secondaryObj.dump();
}

template <typename T>
inline bool ValidateJsonProperty(const nlohmann::json& jObj, const std::string& key, bool required=true)
{
    if(jObj.contains(key) == false)
    {
        return required == false;
    }
    if constexpr (std::is_same<T, int32_t>::value)
    {
        return jObj[key].is_number_integer();
    }
    if constexpr (std::is_same<T, int64_t>::value)
    {
        return jObj[key].is_number_integer();
    }
    else if constexpr (std::is_same<T, float>::value)
    {
        return jObj[key].is_number_float();
    }
    else if constexpr (std::is_same<T, bool>::value)
    {
        return jObj[key].is_boolean();
    }
    else if constexpr (std::is_same<T, const char*>::value)
    {
        return jObj[key].is_string();
    }
    else if constexpr (std::is_same<T, std::string>::value)
    {
        return jObj[key].is_string();
    }
    else if constexpr (std::is_same<T, nlohmann::json>::value)
    {
        return jObj[key].is_object();
    }
    else if constexpr (std::is_same<T, nlohmann::json::array_t>::value)
    {
        return jObj[key].is_array();
    }
    else
    {
        // add more types as needed
        return false;
    }
}

/// @brief Set values in cluttered that do not exist in clean to null.
/// @param clean The json without the intended properties
/// @param cluttered The json with the cluttered properties that need to be set to null
inline void NullifyJsonClutter(nlohmann::json& clean, nlohmann::json& cluttered)
{
    for (nlohmann::json::iterator iter = cluttered.begin(); iter != cluttered.end(); iter++)
    {
        if(clean.contains(iter.key()) == false)
        {
            clean[iter.key()] = nullptr;
        } 
        else if(iter.value().is_object())
        {
            NullifyJsonClutter(clean[iter.key()], iter.value());
        }
    }   
}

template <typename T1, typename T2>
inline bool MapContains(std::map<T1, T2> m, T1 key)
{
    return m.find(key) != m.end();
}

#endif