#include <fstream>

#include <nlohmann/json.hpp>

#include <Panorama/trace.h>
#include <Panorama/comptr.h>
#include <Panorama/flowcontrol.h>

#include <aws/sts/STSClient.h>
#include <aws/sts/model/GetSessionTokenRequest.h>
#include <aws/sts/model/GetSessionTokenResult.h>

#include "../mock_panorama_device.h"

using namespace Panorama;

enum class NodeType
{
    CodeNode = 0,
    StringParameter,
    IntegerParameter,
    BooleanParameter,
    FloatParameter
};

std::vector<std::string> NodeTypeToString
{
    "", "STRING", "INT32", "BOOLEAN", "FLOAT32"
};

#define MESSAGE_COL 70

struct Node
{
    Node() = default;
    Node(const std::string& name)
    {
        Name = name;
    }

    std::string Name;
    std::string Interface;
    NodeType NType = NodeType::CodeNode;

    std::string StrValue;
    int32_t IntValue = 0;
    bool BoolValue = false;
    float FloatValue = 0.0f;

    nlohmann::json ToJson()
    {
        std::stringstream ss;
        nlohmann::json dataList = nlohmann::json::array();
        switch(NType)
        {
            case NodeType::StringParameter:
                dataList.push_back(StrValue);
                break;
            case NodeType::IntegerParameter:
                dataList.push_back(std::to_string(IntValue));
                break;
            case NodeType::BooleanParameter:
                dataList.push_back(BoolValue ? "true" : "false");
                break;
            case NodeType::FloatParameter:
                ss << std::setprecision(3) << FloatValue;
                dataList.push_back(ss.str().find('.') == ss.str().npos ? ss.str() + ".0f" : ss.str() + "f");
                break;
            default:
                break;
        }

        nlohmann::json value;
        value["type"] = NodeTypeToString[static_cast<int32_t>(NType)];
        value["dataList"] = std::move(dataList);

        nlohmann::json json;
        json["name"] = Name;
        json["type"] = NodeTypeToString[static_cast<int32_t>(NType)];
        json["value"] = std::move(value);

        return json;
    }
};

struct NodeCompare 
{
    bool operator()(const Node& a, const Node& b) const 
    {
        return a.Name == b.Name;
    }
};

class AppGraph : public UnknownImpl<IUnknownAlias>
{
public:
    static HRESULT Parse(AppGraph** ppObj, std::string contents)
    {
        HRESULT hr = S_OK;
        CHECKNULL(ppObj, E_POINTER);

        try
        {
            ComPtr<AppGraph> ptr;
            ptr.Attach(new (std::nothrow) AppGraph());

            // Parse the application graph file
            CHECKIF_MSG(nlohmann::json::accept(contents.c_str()) == false, E_INVALIDARG, "Invalid JSON");
            nlohmann::json appGraph = nlohmann::json::parse(contents.c_str());

            CHECKIF_MSG(appGraph.contains("nodeGraph") == false, E_INVALIDARG, "Malformed application graph: No nodeGraph entry");
            nlohmann::json nodeGraph = appGraph["nodeGraph"];

            CHECKIF_MSG(nodeGraph.contains("nodes") == false, E_INVALIDARG, "Malformed application graph: Nodes are not defined");
            nlohmann::json nodes = nodeGraph["nodes"];

            CHECKIF_MSG(nodeGraph.contains("edges") == false, E_INVALIDARG, "Malformed application graph: Edges are not defined");
            nlohmann::json edges = nodeGraph["edges"];

            for(auto &node : nodes.items())
            {
                nlohmann::json node_obj = node.value();
                CHECKIF_MSG(node_obj.contains("name") == false, E_INVALIDARG, "Malformed application graph: Node defined with no name");
                CHECKIF_MSG(node_obj.contains("interface") == false, E_INVALIDARG, "Malformed application graph: Node defined with no interface");

                Node n;
                n.Name = node_obj["name"];
                n.Interface = node_obj["interface"];
                n.NType = NodeType::CodeNode;

                if(node_obj.contains("value"))
                {
                    if(n.Interface == "string")
                    {
                        n.NType = NodeType::StringParameter;
                        n.StrValue = node_obj["value"];
                    }
                    else if(n.Interface == "int32")
                    {
                        n.NType = NodeType::IntegerParameter;
                        n.IntValue = node_obj["value"];
                    }
                    else if(n.Interface == "boolean")
                    {
                        n.NType = NodeType::BooleanParameter;
                        n.BoolValue = node_obj["value"];
                    }
                    else if(n.Interface == "float32")
                    {
                        n.NType = NodeType::FloatParameter;
                        n.FloatValue = node_obj["value"];
                    }
                    else
                    {
                        TraceError("Malformed application graph: Unknown interface type %s", n.Interface.c_str());
                    }
                }

                ptr->_nodes.push_back(n);
            }

            for(auto &edge : edges.items())
            {
                nlohmann::json edge_obj = edge.value();
                
                CHECKIF_MSG(edge_obj.contains("producer") == false, E_INVALIDARG, "Malformed application graph: edge defined with no producer");
                CHECKIF_MSG(edge_obj.contains("consumer") == false, E_INVALIDARG, "Malformed application graph: edge defined with no consumer");
                
                std::string producer = edge_obj["producer"];
                std::string consumer = edge_obj["consumer"];

                // find the producer
                int32_t producerIdx = 0;
                for(; producerIdx < ptr->_nodes.size(); producerIdx++)
                {
                    if(ptr->_nodes[producerIdx].Name == producer)
                    {
                        break;
                    }
                }
                CHECKIF_MSG(producerIdx == ptr->_nodes.size(), E_INVALIDARG, "Malformed application graph: producer not defined in list of nodes");

                // parse the consumer 
                int32_t idx = consumer.find('.');
                CHECKIF_MSG(idx == consumer.npos, E_INVALIDARG, "Malformed application graph: consumer needs to be <node>.<interface>");
                consumer = consumer.substr(0, consumer.find('.'));

                // convert the producer node to json and add to consuer inputPortList json
                if(ptr->_parameters.find(consumer) == ptr->_parameters.end())
                {
                    nlohmann::json inputPortList;
                    inputPortList["inputPortList"] = nlohmann::json::array();
                    ptr->_parameters[consumer] = std::move(inputPortList);
                }

                ptr->_parameters[consumer]["inputPortList"].push_back(ptr->_nodes[producerIdx].ToJson());
            }

            for(const auto &portList : ptr->_parameters)
            {
                ptr->_serializedParameters[portList.first] = portList.second.dump();
            }
            
            *ppObj = ptr.Detach();
            return S_OK;
        }
        catch(const std::exception& e)
        {
            *ppObj = nullptr;
            TraceError("Exception caught in AppGraph::Parse: %s", e.what());
            return E_FAIL;
        }
    }

    const char* InputPortsList(std::string nodeId)
    {
        if(_serializedParameters.find(nodeId) == _serializedParameters.end())
        {
            TraceWarning("Could not find nodeId %s in the app graph", nodeId.c_str());
            return "{}";
        }

        return _serializedParameters[nodeId].c_str();
    }

private:
    std::vector<Node> _nodes;
    std::map<std::string, nlohmann::json> _parameters;
    std::map<std::string, std::string> _serializedParameters;
};

class AppRequestHandler : public UnknownImpl<IAppRequestHandler>
{
public:
    static HRESULT Create(IAppRequestHandler** ppObj, const std::string& appGraphFile)
    {
        HRESULT hr = S_OK;
        CHECKNULL(ppObj, E_POINTER);
        CHECKIF(appGraphFile.empty(), E_INVALIDARG);

        ComPtr<AppRequestHandler> ptr;
        ptr.Attach(new (std::nothrow) AppRequestHandler());
        CHECKNULL(ptr, E_POINTER);

        // Check existence and parse app graph
        {
            std::ifstream fstream;
            fstream.open(appGraphFile);
            if(fstream.is_open() == false)
            {
                TraceError("Could not open application graph file %s", appGraphFile.c_str());
                return E_FAIL;
            }

            std::stringstream contents;
            contents << fstream.rdbuf();
            fstream.close();

            CHECKHR(AppGraph::Parse(ptr->_appGraph.AddressOf(), contents.str()));
        }

        *ppObj = ptr.Detach();
        return hr;
    }

    const char* GetCredentials() override
    {
        Aws::STS::STSClient sts_client;
        Aws::STS::Model::GetSessionTokenRequest request;
        request.SetDurationSeconds(3600);

        Aws::STS::Model::GetSessionTokenOutcome outcome = sts_client.GetSessionToken(request);
        if (outcome.IsSuccess())
        {
            const auto& credentials = outcome.GetResult().GetCredentials();

            nlohmann::json jObj;

            jObj["accessKeyId"] = credentials.GetAccessKeyId().c_str();
            jObj["secretAccessKey"] = credentials.GetSecretAccessKey().c_str();
            jObj["sessionToken"] = credentials.GetSessionToken().c_str();
            jObj["expiration"] = credentials.GetExpiration().ToGmtString(Aws::Utils::DateFormat::ISO_8601_BASIC).c_str();

             _credentials = jObj.dump();
            return _credentials.c_str();
        }
        else
        {
            TraceError("Error getting session token: %s.  Run aws configure.", outcome.GetError().GetMessage().c_str());
        }

        return "{}";
    }

    const char* GetPorts(const char* nodeId) override
    {
        return _appGraph->InputPortsList(nodeId);
    }

    void OnAnnounceSelf(const char* nodeId, const char *version) override
    {
    }

    void OnHeartbeat(const char* nodeId, const char* errorCode, const char* status) override
    {
    }

    void OnTraceMessage(TraceLevel level, Timestamp timestamp, int32_t line, const char* file, const char* message) override
    {
        Panorama::Trace(level, timestamp, line, file, message);
    }

private:
    AppRequestHandler() = default;
    std::string _credentials;
    ComPtr<AppGraph> _appGraph;
};

HRESULT CreateAppRequestHandler(IAppRequestHandler** ppObj, const std::string& appGraphFile)
{
    return AppRequestHandler::Create(ppObj, appGraphFile);
}