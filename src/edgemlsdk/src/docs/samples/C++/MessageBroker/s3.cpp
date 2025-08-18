#include <iostream>

#include <Panorama/trace.h>
#include <Panorama/aws.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/eventing.h>

using namespace Panorama;

int main()
{
    ADD_CONSOLE_TRACE;
    HRESULT hr = S_OK;

    ComPtr<IProtocolClient> client;
    ComPtr<ICredentialProvider> credential_provider;
    ComPtr<IBuffer> contents;
    ComPtr<IPayload> payload;
    ComPtr<IS3Message> message;

    // Get the default AWS credential provider, uses temporary SigV4 credentials
    credential_provider = Panorama_Aws::DefaultCredentialProvider();
    CHECKNULL(credential_provider, E_FAIL);

    // Create the S3 Protocol client
    // You'll need to update the region to something that is appropriate for you
    CHECKHR(Panorama_Aws::S3ProtocolClient(client.AddressOf(), "us-west-2", credential_provider));

    // Create a binary blob with some data
    std::vector<uint8_t> binaryData = { 0x48, 0x65, 0x6C, 0x6C, 0x6F, 0x20, 0x77, 0x6F, 0x72, 0x6C, 0x64, 0x21 };
    Buffer::Create(contents.AddressOf(), binaryData.size());
    memcpy(contents->Data(), binaryData.data(), contents->Size());

    // Create the payload from the buffer
    CHECKHR(MessageBroker::CreatePayload(payload.AddressOf(), contents));
    
    // Create the S3 message from the payload
    // Change the bucket and key your desired location
    CHECKHR(Panorama_Aws::S3Message(message.AddressOf(), payload, "panorama-sdk-v2-artifacts", "sample-data/s3_sample_output.txt"));

    // Synchronously publish the message
    CHECKHR(client->Publish(message));
    TraceInfo("Message published");

    return hr;
}