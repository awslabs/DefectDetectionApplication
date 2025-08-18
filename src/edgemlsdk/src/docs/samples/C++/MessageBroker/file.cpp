#include <Panorama/trace.h>
#include <Panorama/message_broker.h>
#include <Panorama/flowcontrol.h>

using namespace Panorama;

int main()
{
    ADD_CONSOLE_TRACE;
    HRESULT hr = S_OK;

    ComPtr<IProtocolClient> client;
    ComPtr<IPayload> payload;
    ComPtr<IFileProtocolMessage> message;

    // Create the file protocol client
    CHECKHR(MessageBroker::FileProtocolClient(client.AddressOf()));

    // Create a payload
    std::string correlation_id = "123456";
    CHECKHR(MessageBroker::CreatePayload(payload.AddressOf(), "hello world"));
    CHECKHR(payload->SetCorrelationId(correlation_id.c_str()))

    // Create a file protocol message
    CHECKHR(MessageBroker::FileProtocolMessage(message.AddressOf(), payload, "./my-test-directory", "my-file-${c_id}"));

    // Publish the payload
    CHECKHR(client->Publish(message));

    return hr;
}