#ifndef __ENV_VARS_H__
#define __ENV_VARS_H__
#include <string>

inline std::string GetEnvVar(std::string key)
{
#ifdef WINDOWS
    char* value;
    size_t len;
    errno_t err = _dupenv_s(&value, &len, key.c_str());
    if (err)
    {
        return "";
    }

#else
    char* value = std::getenv(key.c_str());
#endif
    
    std::string ret = value == nullptr ? std::string() : value;
    
#ifdef WINDOWS
    free(value);
#endif

    return ret;
}

inline void SetEnvVar(const std::string& key, const std::string& value)
{
#ifdef WINDOWS
    _putenv_s(key.c_str(), value.c_str());
#else
    setenv(key.c_str(), value.c_str(), 1);
#endif
}

inline void UnsetEnvVar(const std::string& key)
{
#ifdef WINDOWS
    _putenv_s(key.c_str(), "");
#else
    unsetenv(key.c_str());
#endif
}

inline std::string GetEnvVar(const std::string& key, const std::string& defaultVal)
{
    std::string val = std::move(GetEnvVar(key));
    return val.empty() ? defaultVal : val;
}

#endif