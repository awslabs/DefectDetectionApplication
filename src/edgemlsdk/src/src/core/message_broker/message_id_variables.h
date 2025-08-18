#ifndef __MESSAGE_ID_VARIABLES_H__
#define __MESSAGE_ID_VARIABLES_H__

#include <vector>
#include <string>
#include <regex>
#include <sstream>

#include <Panorama/flowcontrol.h>

struct MessageIdMatchResults
{
    bool Matches = false;
    std::string MatchedMessageId;
    std::map<std::string, std::string> VariableExpansions;
};

inline HRESULT GetMessageIdVariables(MessageIdMatchResults* pObj, const char* message_id, const std::vector<std::string>& handled_messages)
{
    HRESULT hr = S_OK;
    CHECKNULL(pObj, E_POINTER);
    CHECKNULL_OR_EMPTY(message_id, E_INVALIDARG);

    pObj->Matches = false;
    pObj->MatchedMessageId = "";
    pObj->VariableExpansions.clear();

    for(auto iter = handled_messages.begin(); iter != handled_messages.end(); iter++)
    {
        const std::string& id = *iter;
        size_t prev_idx = 0;
        std::stringstream pattern_stream;
        std::vector<std::string> variable_names;

        // Get the regex pattern for this handled message id
        do
        {
            // find the beginning of a variable expansion
            size_t idx = id.find("${", prev_idx);
            if(idx == std::string::npos)
            {
                // no more variables, add remaining characters to regex pattern
                pattern_stream << id.substr(prev_idx, id.length() - prev_idx);
                break;
            }

            // Add to the regex pattern
            pattern_stream << id.substr(prev_idx, (idx-prev_idx));
            pattern_stream << "(.*)";

            // Find the variable expansion termination
            prev_idx = idx;
            idx = id.find("}", prev_idx);
            CHECKIF_MSG(idx == std::string::npos, E_INVALIDARG, "Unterminated variable expansion");

            // get the name of the variable (contents inside ${})
            std::string var_name = id.substr(prev_idx, (idx - prev_idx) + 1);
            variable_names.push_back(var_name);

            // update prev_idx in preparation for next pass
            prev_idx = idx+1;
            if(prev_idx >= id.length())
            {
                break;
            }
        } while (true);

        // See if message id matches the pattern
        std::regex pattern(pattern_stream.str());
        std::smatch matches;
        std::string input = message_id;
        if (std::regex_search(input, matches, pattern)) 
        {
            pObj->Matches = true;
            pObj->MatchedMessageId = id;
            for (size_t idx = 1; idx < matches.size(); idx++) 
            {
                pObj->VariableExpansions[variable_names[idx - 1]] = std::string(matches[idx]);
            }

            // match found, break
            break;
        }
    }

    return hr;
}

inline void ReplaceAll(std::string& input, const std::string& from, const std::string& to) 
{
    size_t idx = input.find(from, 0);
    while(idx != std::string::npos) 
    {
        input.replace(idx, from.length(), to);
        idx += to.length();
        idx = input.find(from, idx);
    }
}

inline void ExpandMessageOption(std::string& options, const MessageIdMatchResults& results)
{
    for(auto iter = results.VariableExpansions.begin(); iter != results.VariableExpansions.end(); iter++)
    {
        ReplaceAll(options, iter->first, iter->second);
    }
}

#endif