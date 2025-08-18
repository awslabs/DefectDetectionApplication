set(SRC
    buffer.cpp
    chrono.cpp
    memcheck.cpp
    message_broker/batch_payload.cpp
    message_broker/buffer_payload.cpp
    message_broker/file_protocol/file_protocol_client.cpp
    message_broker/file_protocol/file_protocol_factory.cpp
    message_broker/file_protocol/file_protocol_message.cpp
    message_broker/gpio_protocol/gpio_protocol_client.cpp
    message_broker/gpio_protocol/gpio_protocol_factory.cpp
    message_broker/gpio_protocol/gpio_protocol_message.cpp
    message_broker/loopback_protocol.cpp
    message_broker/message_broker.cpp
    message_broker/message_broker_event_handler.cpp
    message_broker/protocol_client_event_handler.cpp
    property/cli_property_delegate.cpp
    property/file_property_delegate.cpp
    property/properties.cpp
    property/property_collection.cpp
    property/property_event_handler.cpp
    property/string_expansion.cpp
    rest/curl.cpp
    rest/request.cpp
    rest/response.cpp
    trace/console_trace_listener.cpp
    trace/file_trace_listener.cpp
    trace/http_trace_listener.cpp
    trace/tracer.cpp
    vector.cpp
    version.cpp
)

add_library(panorama.core SHARED ${SRC})
target_link_libraries(panorama.core CURL::libcurl libperiphery.a)

if(NOT${CMAKE_CXX_COMPILER_VERSION} VERSION_LESS 8.0)
    # file_trace_listener uses the std::experimental::fileystem on Ubuntu 18.04
    # which requires linking to stdc++fs
    target_link_libraries(panorama.core stdc++fs)
endif()


file(GLOB publicHeaders "${PROJECT_SOURCE_DIR}/include/Panorama/*.h")
set_target_properties(panorama.core PROPERTIES PUBLIC_HEADER "${publicHeaders}")
install(TARGETS panorama.core PUBLIC_HEADER DESTINATION include/Panorama)
