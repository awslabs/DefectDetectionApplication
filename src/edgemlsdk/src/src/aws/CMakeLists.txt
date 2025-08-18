set(SRC
    aws_context.cpp
    default_credential_provider.cpp
    iot_shadow_property_delegate.cpp
    mqtt/mqtt_message.cpp
    mqtt/mqtt_protocol_client.cpp
    mqtt/mqtt_protocol_factory.cpp
    mqtt/mqtt_subscription.cpp
    s3/s3_expansion.cpp
    s3/s3_property_delegate.cpp
    s3/s3_protocol_client.cpp
    s3/s3_protocol_client_factory.cpp
    s3/s3_protocol_message.cpp
    secrets_manager_expansion.cpp
    )

add_library(panorama.aws SHARED ${SRC})
target_link_libraries(panorama.aws panorama.core aws-cpp-sdk-core aws-cpp-sdk-secretsmanager aws-cpp-sdk-iot aws-cpp-sdk-iot-data aws-cpp-sdk-s3 aws-crt-cpp aws-cpp-sdk-sts aws-cpp-sdk-transfer)

if(NOT${CMAKE_CXX_COMPILER_VERSION} VERSION_LESS 8.0)
    # s3_expansion uses the std::experimental::fileystem on Ubuntu 18.04
    # which requires linking to stdc++fs
    target_link_libraries(panorama.aws stdc++fs)
endif()

install(TARGETS panorama.aws)